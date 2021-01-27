import tensorflow as tf
import tensorflow.contrib.slim as slim

from libs.config import load_config
from nets.cnn.mobile_net_v2 import MobileNetV2
from nets.cnn.paper_cnn import PaperCNN
from nets.cnn.dense_net import DenseNet
from nets.cnn.squeeze_net import SqueezeNet
from nets.cnn.resnet_v2 import ResNetV2
from nets.cnn.simple_net import SimpleNet
import numpy as np
from PIL import Image
import copy

class CRNN(object):
    CTC_INVALID_INDEX = -1

    def __init__(self, cfg, num_classes):
        self.inputs = tf.placeholder(tf.float32,
                                     [None, 32, None, 1],
                                     name="inputs")
        self.cfg = cfg
        # SparseTensor required by ctc_loss op
        self.labels = tf.sparse_placeholder(tf.int32, name="labels")
        self.con_labels = tf.placeholder(tf.int32, name="con_labels")
        self.len_labels = tf.placeholder(tf.int32, name="len_labels")
        # 1d array of size [batch_size]
        self.is_training = tf.placeholder(tf.bool, name="is_training")

        self.num_classes = num_classes

        self._build_model()
        self._build_train_op()

        self.merged_summay = tf.summary.merge_all()

    def _build_model(self):
        if self.cfg.name == 'raw':
            net = PaperCNN(self.inputs, self.is_training)
        elif self.cfg.name == 'dense':
            net = DenseNet(self.inputs, self.is_training)
        elif self.cfg.name == 'squeeze':
            net = SqueezeNet(self.inputs, self.is_training)
        elif self.cfg.name == 'resnet':
            net = ResNetV2(self.inputs, self.is_training)
        elif self.cfg.name == 'simple':
            net = SimpleNet(self.inputs, self.is_training)
        elif self.cfg.name == 'mobile':
            net = MobileNetV2(self.inputs, self.is_training)

        # tf.reshape() vs Tensor.set_shape(): https://stackoverflow.com/questions/35451948/clarification-on-tf-tensor-set-shape
        # tf.shape() vs Tensor.get_shape(): https://stackoverflow.com/questions/37096225/how-to-understand-static-shape-and-dynamic-shape-in-tensorflow
        cnn_out = net.net
        self.cnn_out = cnn_out
        cnn_output_shape = tf.shape(cnn_out)# 32 , 4, 64, 1024



        print('tf.shape(cnn_out):',tf.shape(cnn_out))
        batch_size = cnn_output_shape[0]
        self.batch_size = batch_size
        cnn_output_h = cnn_output_shape[1]
        cnn_output_w = cnn_output_shape[2]
        cnn_output_channel = cnn_output_shape[3]

        # Get seq_len according to cnn output, so we don't need to input this as a placeholder
        self.seq_len = tf.ones([batch_size], tf.int32) * cnn_output_w

        # Reshape to the shape lstm needed. [batch_size, max_time, ..]
        cnn_out_transposed = tf.transpose(cnn_out, [0, 2, 1, 3])
        cnn_out_reshaped = tf.reshape(cnn_out_transposed, [batch_size, cnn_output_w, cnn_output_h * cnn_output_channel])

        cnn_shape = cnn_out.get_shape().as_list()
        cnn_out_reshaped.set_shape([None, cnn_shape[2], cnn_shape[1] * cnn_shape[3]])

        # 32,w/4,6490 -> 32,w/4,2
        self.pos_logits = slim.fully_connected(cnn_out_reshaped, 2, activation_fn=None)

        self.outputs_pos = tf.reshape(self.pos_logits, [-1, 2])


    def unpool(inputs):
        return tf.image.resize_bilinear(inputs, size=[tf.shape(inputs)[1] * 4, tf.shape(inputs)[2]])

    def _build_train_op(self):
        self.global_step = tf.Variable(0, trainable=False)


        self.pos_loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(logits=self.outputs_pos,labels=self.con_labels))


        self.total_loss = self.pos_loss

        # tf.summary.scalar('ctc_loss', self.ctc_loss)
        # tf.summary.scalar('regularization_loss', self.regularization_loss)
        tf.summary.scalar('total_loss', self.total_loss)

        # self.lr = tf.train.exponential_decay(self.cfg.lr,
        #                                      self.global_step,
        #                                      self.cfg.lr_decay_steps,
        #                                      self.cfg.lr_decay_rate,
        #                                      staircase=True)
        #print(self.cfg.lr_boundaries)
        #print(self.cfg.lr_values)
        self.lr = tf.train.piecewise_constant(self.global_step, self.cfg.lr_boundaries, self.cfg.lr_values)

        tf.summary.scalar("learning_rate", self.lr)

        if self.cfg.optimizer == 'adam':
            self.optimizer = tf.train.AdamOptimizer(learning_rate=self.lr)
        elif self.cfg.optimizer == 'rms':
            self.optimizer = tf.train.RMSPropOptimizer(learning_rate=self.lr,
                                                       epsilon=1e-8)
        elif self.cfg.optimizer == 'adadelate':
            self.optimizer = tf.train.AdadeltaOptimizer(learning_rate=self.lr,
                                                        rho=0.9,
                                                        epsilon=1e-06)
        elif self.cfg.optimizer == 'sgd':
            self.optimizer = tf.train.MomentumOptimizer(learning_rate=self.lr,
                                                        momentum=0.9)

        # required by batch normalize
        # add update ops(for moving_mean and moving_variance) as a dependency to the train_op
        # https://www.tensorflow.org/api_docs/python/tf/layers/batch_normalization
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        # update_ops.append(centers_update_op)
        with tf.control_dependencies(update_ops):
            self.train_op = self.optimizer.minimize(self.total_loss, global_step=self.global_step)


    def _LSTM_cell(self, num_proj=None):
        cell = tf.nn.rnn_cell.LSTMCell(num_units=self.cfg.rnn_num_units, num_proj=num_proj)
        if self.cfg.rnn_keep_prob < 1:
            cell = tf.contrib.rnn.DropoutWrapper(cell=cell, output_keep_prob=self.cfg.rnn_keep_prob)
        return cell

    def _paper_bidirectional_LSTM(self, inputs, num_proj):
        """
            根据 CRNN BiRnnJoin.lua 源码改写
        :param inputs: shape [batch_size, max_time, ...]
        :param num_proj: 每个 cell 输出的维度
        :return: shape [batch_size, max_time, num_proj]
        """
        (blstm_fw, blstm_bw), _ = tf.nn.bidirectional_dynamic_rnn(self._LSTM_cell(num_proj=num_proj),
                                                                  self._LSTM_cell(num_proj=num_proj),
                                                                  inputs,
                                                                  sequence_length=self.seq_len,
                                                                  dtype=tf.float32)
        return tf.add(blstm_fw, blstm_bw)

    def _bidirectional_LSTM(self, inputs, num_out):
        #numout == 6941
        outputs, _ = tf.nn.bidirectional_dynamic_rnn(self._LSTM_cell(),
                                                     self._LSTM_cell(),
                                                     inputs,
                                                     sequence_length=self.seq_len,
                                                     dtype=tf.float32)

        outputs = tf.concat(outputs, 2)
        outputs = tf.reshape(outputs, [-1, self.cfg.rnn_num_units * 2])

        outputs = slim.fully_connected(outputs, num_out, activation_fn=None)

        shape = tf.shape(inputs)
        outputs = tf.reshape(outputs, [shape[0], -1, num_out])

        return outputs

    def fetches(self):
        """
        Return operations to fetch for inference
        """
        return [
            self.outputs_pos,
        ]


    def feeds(self):
        """
        Return placeholders to feed for inference
        """
        return {'inputs': self.inputs,
                'labels': self.labels,
                'con_labels':self.con_labels,
                'len_labels':self.len_labels,
                'is_training': self.is_training}
