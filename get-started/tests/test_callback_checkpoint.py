# Copyright (c) 2022 Graphcore Ltd. All rights reserved.
import inspect
from datetime import datetime
from glob import glob
from pathlib import Path
from tempfile import TemporaryDirectory

import tensorflow as tf
from tensorflow.python import ipu

from keras_extensions.callbacks.checkpoint_callback import CheckpointCallback


class TestCheckpointCallbacks:

    def test_checkpoint_creation(self):
        with TemporaryDirectory() as temp_dir:
            checkpoint_name = (f"{inspect.currentframe().f_code.co_name}"
                               f"_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            checkpoint_dir = Path(temp_dir).joinpath(checkpoint_name).joinpath("test")

            micro_batch_size = 4
            steps_per_execution = 5
            num_replicas = 2
            total_num_micro_batches = 130
            executions_per_ckpt = 6

            ds = tf.data.Dataset.from_tensor_slices(([1.0, 1.0], [1.0, 2.0]))
            ds = ds.repeat()
            ds = ds.batch(micro_batch_size, drop_remainder=True)

            cfg = ipu.config.IPUConfig()
            cfg.auto_select_ipus = num_replicas
            cfg.device_connection.type = ipu.utils.DeviceConnectionType.ON_DEMAND
            cfg.configure_ipu_system()

            callback = CheckpointCallback(checkpoint_dir=checkpoint_dir,
                                          executions_per_ckpt=executions_per_ckpt)

            strategy = ipu.ipu_strategy.IPUStrategy()
            with strategy.scope():
                optimizer = tf.keras.optimizers.SGD(learning_rate=0.1)
                input_layer = tf.keras.Input(shape=1)
                x = tf.keras.layers.Dense(1, use_bias=False)(input_layer)
                model = tf.keras.Model(input_layer, x)
                model.build(input_shape=(1, 1))
                model.compile(optimizer=optimizer,
                              loss=tf.keras.losses.MSE,
                              steps_per_execution=steps_per_execution)
                model.fit(ds,
                          steps_per_epoch=total_num_micro_batches,
                          epochs=1,
                          callbacks=callback)

            checkpoint_files = glob(str(checkpoint_dir.joinpath("*.h5")))

            print(checkpoint_files)

            # 2 checkpoints created at 60 and 120 micro batches
            # 1 checkpoint created at end of training (130 micro batches)
            assert(len(checkpoint_files) == 3)
