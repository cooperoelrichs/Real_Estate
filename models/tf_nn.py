import os
import sys

import numpy as np
from sklearn.metrics import r2_score

import tensorflow as tf
tf.logging.set_verbosity(tf.logging.INFO)
os.environ['TF_CPP_MIN_VLOG_LEVEL'] = '3'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

from tensorflow.python.framework.errors_impl import NotFoundError

from real_estate.models.simple_nn import (
    NN, SimpleNeuralNetworkModel, EmptyKerasModel)
from real_estate.models.price_model import PriceModel
from real_estate.tf_utilities.train_and_evaluate import train_and_evaluate
from real_estate.tf_utilities import python3_compatibility_hacks
from real_estate.tf_utilities.validation_hook import ValidationHook


class TFNNModel(SimpleNeuralNetworkModel):
    USE_TPU = False

    def __init__(
        self, learning_rate, learning_rate_decay, momentum,
        lambda_l1, lambda_l2, max_norm, batch_normalization, dropout_fractions,
        input_dim, epochs, batch_size, validation_split,
        layers, optimiser,
        outputs_dir, bucket_dir, steps_between_evaluations
    ):
        self.input_dim = input_dim
        self.layers = layers
        self.learning_rate = learning_rate
        self.learning_rate_decay = learning_rate_decay
        self.momentum = momentum
        self.lambda_l1 = lambda_l1
        self.lambda_l2 = lambda_l2
        self.max_norm = max_norm
        self.batch_normalization = batch_normalization
        self.dropout_fractions = dropout_fractions
        self.epochs = epochs
        self.batch_size = batch_size
        self.optimiser_name = optimiser
        self.validation_split = validation_split
        self.steps_between_evaluations = steps_between_evaluations

        if self.USE_TPU:
            model_dir = os.path.join(bucket_dir, 'model')
        else:
            model_dir = os.path.join(outputs_dir, 'model')

        self.model_dir = model_dir
        # self.train_dir = os.path.join(model_dir, 'train')
        # self.eval_dir = os.path.join(model_dir, 'eval')

        self.del_model_dir()
        self.mk_model_dir()

        # TODO:
        # self.verbosity
        # self.activation
        # self.kernel_initializer
        # self.loss
        # self.optimizer

        self.model_checks()

    def model_checks(self):
        if self.dropout_fractions is not None and (
            len(self.layers) != len(self.dropout_fractions)
        ):
            raise ValueError('Layers and dropout fractions are not consistant.')

    def del_model_dir(self):
        try:
            tf.gfile.DeleteRecursively(self.model_dir)
        except NotFoundError:
            pass

    def mk_model_dir(self):
        tf.gfile.MkDir(self.model_dir)

    def compile_model(self):
        if self.USE_TPU:
            tpu_cluster_resolver = tf.contrib.cluster_resolver.TPUClusterResolver(
                # tpu=[os.environ['TPU_NAME']]
                tpu='c-oelrichs'
            )

            run_config = tf.contrib.tpu.RunConfig(
                cluster=tpu_cluster_resolver,
                model_dir=self.model_dir,
                session_config=tf.ConfigProto(
                    allow_soft_placement=True, log_device_placement=True
                ),
                tpu_config=tf.contrib.tpu.TPUConfig(num_shards=8)
            )

            estimator = tf.contrib.tpu.TPUEstimator(
                model_fn=self.build_model_fn(),
                use_tpu=self.USE_TPU,
                train_batch_size=self.batch_size,
                eval_batch_size=self.batch_size,
                predict_batch_size=self.batch_size,
                model_dir=self.model_dir,
                config=run_config
            )
        else:
            run_config = tf.contrib.tpu.RunConfig(
                model_dir=self.model_dir,
                session_config=tf.ConfigProto(
                    allow_soft_placement=True, log_device_placement=True
                ),
            )
            estimator = tf.contrib.tpu.TPUEstimator(
                model_fn=self.build_model_fn(),
                use_tpu=self.USE_TPU,
                train_batch_size=self.batch_size,
                eval_batch_size=self.batch_size,
                predict_batch_size=self.batch_size,
                model_dir=self.model_dir,
                config=run_config
            )
        return estimator

    def build_model_fn(self):
        def model_fn(features, labels, mode, config, params):
            model = self.model_tensor(features)

            if mode == tf.estimator.ModeKeys.PREDICT:
                return tf.contrib.tpu.TPUEstimatorSpec(
                    mode=mode,
                    predictions={'predictions': model}
                )
            elif mode == tf.estimator.ModeKeys.TRAIN:
                loss = self.loss_tensor(model, labels)
                # self.summary_tensors('train-summaries', model, labels, loss)

                if self.optimiser_name == 'sgd':
                    learning_rate = tf.train.inverse_time_decay(
                        learning_rate=self.learning_rate,
                        global_step=tf.train.get_global_step(),
                        decay_steps=1,
                        decay_rate=self.learning_rate_decay,
                    )
                    optimizer = tf.train.MomentumOptimizer(
                        learning_rate=learning_rate,
                        momentum=self.momentum,
                        use_nesterov=True
                    )
                    optimizer = self.maybe_to_tpu_optimizer(optimizer)

                    clipped_grad_var_pairs = [
                        (self.clip_by_value(dx, -1., 1.), x)
                        for dx, x in optimizer.compute_gradients(loss)
                    ]
                    train_op = optimizer.apply_gradients(
                        clipped_grad_var_pairs,
                        global_step=tf.train.get_global_step()
                    )

                elif self.optimiser_name == 'adam':
                    optimizer = tf.train.AdamOptimizer(
                        learning_rate=self.learning_rate,
                    )
                    optimizer = self.maybe_to_tpu_optimizer(optimizer)
                    train_op = optimizer.minimize(
                        loss, global_step=tf.train.get_global_step()
                    )

                return tf.contrib.tpu.TPUEstimatorSpec(
                    mode=mode,
                    loss=loss,
                    train_op=train_op,
                    predictions={'predictions': model},
                )
            elif mode == tf.estimator.ModeKeys.EVAL:
                loss = self.loss_tensor(model, labels)
                # self.summary_tensors('eval-summaries', model, labels, loss)
                return tf.estimator.EstimatorSpec(
                    mode=mode,
                    loss=loss,
                    predictions={'predictions': model},
                    eval_metric_ops={
                        'eval-summaries/mse': tf.metrics.mean_squared_error(labels, model),
                        'eval-summaries/mae': tf.metrics.mean_absolute_error(labels, model),
                        'eval-summaries/r2': self.r2_metric(labels, model)
                    }
                )
            else:
                raise ValueError("Mode '%s' not supported." % mode)
        return model_fn

    def clip_by_value(self, tensor, min_val, max_val):
        return tf.clip_by_value(tensor, min_val, max_val)

    def maybe_to_tpu_optimizer(self, optimizer):
        if self.USE_TPU:
            return tf.contrib.tpu.CrossShardOptimizer(optimizer)
        else:
            return optimizer

    def model_tensor(self, model):
        self.model_checks()
        regularizer = tf.contrib.layers.l1_l2_regularizer
        kernel_initializer = tf.initializers.random_uniform

        for i, units in enumerate(self.layers):
            model = tf.layers.Dense(
                units=units,
                activation=None,
                kernel_initializer=kernel_initializer(),
                kernel_regularizer=regularizer(self.lambda_l1, self.lambda_l2),
                kernel_constraint=self.maybe_max_norm(),
            )(model)

            if self.batch_normalization is True:
                model = tf.layers.BatchNormalization()(model)

            model = tf.keras.layers.PReLU()(model)

            if self.dropout_fractions is not None:
                model = tf.layers.Dropout(self.dropout_fractions[i])(model)

        model = tf.layers.Dense(
            units=1,
            activation=None,
            kernel_initializer=kernel_initializer(),
            kernel_regularizer=regularizer(self.lambda_l1, self.lambda_l2)
        )(model)

        model = model[:, 0]
        return model

    def loss_tensor(self, model, labels):
        return tf.losses.mean_squared_error(
            labels=labels,
            predictions=model
        )

    def maybe_max_norm(self):
        if self.max_norm:
            return tf.keras.constraints.MaxNorm(self.max_norm)
        else:
            return None


    def summary_tensors(self, name_space, model, labels, loss):
        with tf.name_scope(name_space):
            tf.summary.scalar('mse', self.mse_value(model, labels))
            tf.summary.scalar('mae', self.mae_value(model, labels))
            tf.summary.scalar('r2', self.r2_value(model, labels))

    # def metrics_fn(self, labels, predictions):
    #     return {
    #         'mae': tf.metrics.mean_absolute_error(labels, predictions),
    #         'r2': self.r2_metric(labels, predictions)
    #     }

    def mse_value(self, model, labels):
        return tf.reduce_mean(tf.square(tf.subtract(labels, model)))

    def mae_value(self, model, labels):
        return tf.reduce_mean(tf.abs(tf.subtract(labels, model)))

    def r2_value(self, model, labels):
        sse = self.mae_value(model, labels)
        sst = self.mae_value(
            tf.fill(tf.shape(labels), tf.reduce_mean(labels)), labels
        )
        return tf.subtract(1.0, tf.div(sse, sst))

    def r2_metric(self, labels, predictions):
        sse, update_op1 = tf.metrics.mean_squared_error(labels, predictions)
        sst, update_op2 = tf.metrics.mean_squared_error(
            labels, tf.fill(tf.shape(labels), tf.reduce_mean(labels))
        )
        r2_value = tf.subtract(1.0, tf.div(sse, sst))
        return r2_value, tf.group(update_op2, update_op1)

    def fit(self, X, y):
        self.x_scaler, X_scaled = self.new_scaler(X)
        X_scaled = X_scaled.astype(np.float32)
        y = y.astype(np.float32)

        validation_split = int(X.shape[0] * self.validation_split)
        X_train = X_scaled[validation_split:]
        X_valid = X_scaled[:validation_split]
        y_train = y[validation_split:]
        y_valid = y[:validation_split]

        train_ds_dir = self.save_train_dataset(X_train, y_train, self.model_dir)
        eval_ds_dir = self.save_eval_dataset(X_valid, y_valid, self.model_dir)

        self.model = self.compile_model()
        train_input_fn = self.make_train_input_fn(train_ds_dir, self.epochs)
        eval_input_fn = self.make_train_input_fn(eval_ds_dir, 1)

        training_steps = int(
            X_train.shape[0] * (1 - self.validation_split) /
            self.batch_size * self.epochs
        )

        evaluation_steps = int(X_train.shape[0] / self.batch_size)
        hooks = self.add_hooks_for_validation([], eval_ds_dir)
        self.model.train(
            input_fn=train_input_fn,
            max_steps=training_steps,
            hooks=hooks
        )

    def evaluate(self, X_test, y_test):
        X_scaled = self.x_scaler.transform(X_test)
        X_scaled = X_scaled.astype(np.float32)
        y_test = y_test.astype(np.float32)

        input_fn = self.make_test_input_fn(
            X_scaled, self.batch_size
        )

        loss_and_metrics = self.model.evaluate(
            input_fn,
            steps=None,
            hooks=None,
            checkpoint_path=None,
            name=None
        )
        return loss_and_metrics

    def score(self, X_test, y_test):
        X_scaled = self.x_scaler.transform(X_test)
        X_scaled = X_scaled.astype(np.float32)
        y_test = y_test.astype(np.float32)

        predict_ds_dir = self.save_predict_dataset(
            X_scaled, self.model_dir
        )
        predict_input_fn = self.make_predict_input_fn(
            predict_ds_dir
        )

        y_pred = self.model.predict(predict_input_fn)
        y_pred = np.array([a['predictions'] for a in y_pred])
        return r2_score(y_test, y_pred)

    # def predict(self, X_pred):
    #     X_scaled = self.x_scaler.transform(X_pred)
    #     X_scaled = X_scaled.astype(np.float32)
    #     y_pred = self.model.predict(input_fn, yield_single_examples=False)
    #     return y_pred

    def add_hooks_for_validation(self, hooks, eval_ds):

        validation_input_fn = self.make_train_input_fn(
            eval_ds, 1
        )
        return hooks + [
            tf.train.CheckpointSaverHook(
                checkpoint_dir=self.model_dir,
                save_steps=self.steps_between_evaluations
            ),
            ValidationHook(
                self.model, self.build_model_fn(),
                {'batch_size': self.batch_size}, self.batch_size,
                validation_input_fn, self.model_dir,
                self.USE_TPU,
                every_n_steps=self.steps_between_evaluations,
            )
        ]

    def save_train_dataset(self, X, y, run_dir):
        return self.save_tf_dataset(
            X, y, run_dir, tf.estimator.ModeKeys.TRAIN
        )

    def save_eval_dataset(self, X, y, run_dir):
        return self.save_tf_dataset(
            X, y, run_dir, tf.estimator.ModeKeys.EVAL
        )

    def save_predict_dataset(self, X, run_dir):
        return self.save_tf_dataset(
            X, None, run_dir, tf.estimator.ModeKeys.PREDICT
        )

    def save_tf_dataset(self, X, y, run_dir, mode):
        data_file_path = os.path.join(run_dir, 'data-' + mode + '.tfrecords')
        print(data_file_path)

        with tf.python_io.TFRecordWriter(data_file_path) as writer:
            for i in range(X.shape[0]):
                if (mode == tf.estimator.ModeKeys.TRAIN or
                    mode == tf.estimator.ModeKeys.EVAL):
                    feature={'X': tf.train.Feature(
                             float_list=tf.train.FloatList(value=X[i])),
                             'y': tf.train.Feature(
                             float_list=tf.train.FloatList(value=[y[i]]))}
                elif mode == tf.estimator.ModeKeys.PREDICT:
                    feature={'X': tf.train.Feature(
                             float_list=tf.train.FloatList(value=X[i]))}

                writer.write(tf.train.Example(
                    features=tf.train.Features(feature=feature)
                ).SerializeToString())

        return data_file_path

    def make_train_input_fn(self, ds_dir, epochs):
        return self.make_input_fn(
            ds_dir, epochs, tf.estimator.ModeKeys.TRAIN
        )

    def make_predict_input_fn(self, ds_dir):
        return self.make_input_fn(
            ds_dir, 1, tf.estimator.ModeKeys.PREDICT
        )

    def make_input_fn(self, data_file_path, epochs, mode):
        X_WIDTH = 16
        assert tf.gfile.Exists(data_file_path)

        def decode_x_and_y(example):
            features = {
                'X': tf.FixedLenSequenceFeature(
                    shape=(X_WIDTH,), dtype=tf.float32, allow_missing=True),
                'y': tf.FixedLenSequenceFeature(
                    shape=(1,), dtype=tf.float32, allow_missing=True)
            }
            parsed_features = tf.parse_single_example(example, features)
            return (parsed_features['X'][0], parsed_features['y'][0, 0])

        def decode_x_only(example):
            features = {
                'X': tf.FixedLenSequenceFeature(
                    shape=(X_WIDTH,), dtype=tf.float32, allow_missing=True),
            }
            parsed_features = tf.parse_single_example(example, features)
            return parsed_features['X'][0]

        def input_fn(params):
            batch_size = params['batch_size']
            ds = tf.data.TFRecordDataset(data_file_path)

            if (mode == tf.estimator.ModeKeys.TRAIN or
                mode == tf.estimator.ModeKeys.EVAL):
                ds = ds.map(decode_x_and_y, num_parallel_calls=8)
            elif mode == tf.estimator.ModeKeys.PREDICT:
                ds = ds.map(decode_x_and_y, num_parallel_calls=8)

            ds = ds.cache()
            ds = ds.apply(tf.contrib.data.shuffle_and_repeat(batch_size*10, epochs))
            ds = ds.apply(tf.contrib.data.batch_and_drop_remainder(batch_size))
            ds = ds.prefetch(buffer_size=1)

            iterator = ds.make_one_shot_iterator()
            batch = iterator.get_next()

            if (mode == tf.estimator.ModeKeys.TRAIN or
                mode == tf.estimator.ModeKeys.EVAL):
                X_batch, y_batch = batch
                return X_batch, y_batch
            elif mode == tf.estimator.ModeKeys.PREDICT:
                return batch
        return input_fn

    ####
    # batch_size = 3
    # ds = ds.cache().repeat().shuffle(buffer_size=50000).apply(
    #     tf.contrib.data.batch_and_drop_remainder(batch_size)
    # )
    # batch = ds.make_one_shot_iterator().get_next()
    #
    # sess = tf.Session()
    # print(sess.run(batch))
    # exit()
    ####

    # def new_scaler(self, x):
    # def empty_scaler(self, x):
    # def score(self, X_test, y_test):
    # def r2(y_true, y_pred):
    # def unscale(x, mean, scale):
    # def mae(y_true, y_pred):
    # def mse(y_true, y_pred):
    # def smooth_l1(y_true, y_pred):
    # def scaled_mae(y_scaler):
    # def scaled_mse(y_scaler):
    # def simple_lr_scheduler(learning_rate):


class TFNN(NN):
    MODEL_CLASS = TFNNModel

    PARAMS = {
        'input_dim': None,
        'layers': None,
        'learning_rate': None,
        'learning_rate_decay': None,
        'momentum': None,
        'lambda_l1': None,
        'lambda_l2': None,
        'max_norm': None,
        'batch_normalization': None,
        'dropout_fractions': None,
        'epochs': None,
        'batch_size': None,
        'optimiser': 'sgd',
        'validation_split': 0.2,
        'steps_between_evaluations': 1000,

        'outputs_dir': None,
        'bucket_dir': 'gs://real-estate-modelling-temp-bucket/model',
    }

    def show_live_results(self, outputs_folder, name):
        PriceModel.show_live_results(self, outputs_folder, name)

    def model_summary(self):
        PriceModel.model_summary(self)
        # tf.summary.tensor_summary(
        #     'Model',
        #     self.MODEL_CLASS.loss_tensor(
        #         tf.placeholder(np.float32, shape=(
        #             self.model.batch_size, self.model.input_dim
        #         )),
        #         tf.placeholder(np.float32, shape=(self.model.batch_size,))
        #     ),
        #     summary_description=None,
        #     collections=None,
        #     summary_metadata=None,
        #     family=None,
        #     display_name=None
        # )
