import tensorflow as tf


class ValidationHook(tf.train.SessionRunHook):
    def __init__(
        self, estimator, model_fn, params, batch_size, input_fn,
        checkpoint_dir, use_tpu, every_n_secs=None, every_n_steps=None
    ):
        self._iter_count = 0

        if use_tpu:
            tpu_cluster_resolver = tf.contrib.cluster_resolver.TPUClusterResolver(
                tpu='c-oelrichs'
            )

            run_config = tf.contrib.tpu.RunConfig(
                cluster=tpu_cluster_resolver,
                model_dir=checkpoint_dir,
                session_config=tf.ConfigProto(
                    allow_soft_placement=True, log_device_placement=True
                ),
                tpu_config=tf.contrib.tpu.TPUConfig()
            )
        else:
            run_config = tf.contrib.tpu.RunConfig(
                model_dir=checkpoint_dir,
                session_config=tf.ConfigProto(
                    allow_soft_placement=True, log_device_placement=True
                ),
            )

        self._eval_estimator = tf.contrib.tpu.TPUEstimator(
            model_fn=model_fn,
            use_tpu=False,
            train_batch_size=batch_size,
            eval_batch_size=batch_size,
            model_dir=checkpoint_dir,
            config=run_config
        )

        # self._eval_estimator = tf.estimator.Estimator(
        #     model_fn=model_fn,
        #     params=params,
        #     model_dir=checkpoint_dir
        # )

        # self._eval_estimator = estimator


        self._input_fn = input_fn
        self._timer = tf.train.SecondOrStepTimer(every_n_secs, every_n_steps)
        self._should_trigger = False

    def begin(self):
        self._timer.reset()
        self._iter_count = 0

    def before_run(self, run_context):
        self._should_trigger = self._timer.should_trigger_for_step(self._iter_count)

    def after_run(self, run_context, run_values):
        if self._should_trigger:
            print('Running an evaluation epoch.')
            self._eval_estimator.evaluate(self._input_fn)
            self._timer.update_last_triggered_step(self._iter_count)
            print('Evaluation complete.')
        self._iter_count += 1
