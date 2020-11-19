import argparse
import glob
import numpy as np
import os
from pathlib import Path
import pickle
import h5py
import tensorflow as tf
import matplotlib.pyplot as plt
from tensorflow.keras.callbacks import *
from tensorflow.keras.losses import *
from tensorflow.keras.metrics import *
from tensorflow.keras.optimizers import *
from tensorflow.keras.layers import Dropout
import tensorflow.keras.backend as K
from tensorflow.keras.utils import multi_gpu_model
from tensorflow.python.keras.saving import hdf5_format

import librosa
import efficientnet.model as model
from swa import SWA
from pipeline import *
from transforms import *
from utils import *
from data_utils import *
#from data_utils import *
from metrics import *
from models import transformer_layer, encoder

from train_density_full import Spec_Filter


np.set_printoptions(precision=4)

args = argparse.ArgumentParser()
args.add_argument('--name', type=str, required=True)
args.add_argument('--model', type=str, default='EfficientNetB0')
args.add_argument('--gpus', type=int, default=[0], nargs='+')
args.add_argument('--mode', type=str, default='GRU',
                                 choices=['GRU', 'transformer'])
args.add_argument('--pretrain', type=bool, default=False)
args.add_argument('--n_layers', type=int, default=0)
args.add_argument('--n_frame', type=int, default=2048)
args.add_argument('--n_dim', type=int, default=256)
args.add_argument('--n_heads', type=int, default=8)
args.add_argument('--n_classes', type=int, default=30)
args.add_argument('--n_mels', type=int, default=128)

args.add_argument('--test_swa', default=False, action='store_true')

# TRAINING
args.add_argument('--l2', type=float, default=1e-6)

args.add_argument('--multiplier', type=float, default=10.)


def safe_div(x, y, eps=EPSILON):
    # returns safe x / max(y, epsilon)
    return x / tf.maximum(y, eps)


def make_extractor(config):
    #spec_filter = Spec_Filter()
    mel_matrix = tf.signal.linear_to_mel_weight_matrix(
        config.n_mels, 257, 16000)

    def extract(complex_tensor, y=None):
        # complex_to_magphase
        n_chan = 2
        real = complex_tensor[..., :n_chan]
        img = complex_tensor[..., n_chan:]

        mag = tf.math.sqrt(real**2 + img**2)
        phase = tf.math.atan2(img, real)

        mel = tf.tensordot(mag, mel_matrix, axes=[-3, 0]) # [b, time, chan, mel]

        if len(x.shape) == 4:
            mel = tf.transpose(mel, perm=[0, 3, 1, 2])
        elif len(x.shape) == 3:
            mel = tf.transpose(mel, perm=[2, 0, 1])
        else:
            raise ValueError('len(x.shape) must be 3 or 4')

        '''
        axis = tuple(range(1, len(mag.shape)))

        mag_max = tf.math.reduce_max(mag, axis=axis, keepdims=True) # MIN-MAX
        mag_min = tf.math.reduce_min(mag, axis=axis, keepdims=True)
        logmag = safe_div(mag-mag_min, mag_max-mag_min)

        logmag = tf.math.log(logmag + EPSILON)
        logmag1 = logmag * -1.
        logmag2 = logmag + 18.420680743952367
        logmag3 = tf.abs(tf.abs(logmag + logmag2) - 18.420680743952367)
        logmags = tf.concat([logmag1, logmag2, logmag3], axis=-1)

        # magphase_to_mel [b, time, chan, mel]
        mel = spec_filter(logmags)
        '''

        # minmax_log_on_mel
        
        axis = tuple(range(1, len(mel.shape)))
        
        mel_max = tf.math.reduce_max(K.stop_gradient(mel), axis=axis, keepdims=True) # MIN-MAX
        mel_min = tf.math.reduce_min(K.stop_gradient(mel), axis=axis, keepdims=True)
        mel = safe_div(mel-mel_min, mel_max-mel_min)
        
        mel = tf.math.log(mel + EPSILON)    # LOG
        mel1 = mel * -1.
        mel2 = mel + 18.420680743952367
        mel3 = tf.abs(tf.abs(mel + mel2) - 18.420680743952367)
        mel = tf.concat([mel1, mel2, mel3], axis=-1)
        

        # normalized complex
        # n_real = safe_div(real, mel_max)
        # n_img = safe_div(img, mel_max)
        # comp = tf.concat([n_real, n_img], axis=-1)

        # mag_max = tf.math.reduce_max(mag, axis=axis, keepdims=True) # MIN-MAX
        # mag_min = tf.math.reduce_min(mag, axis=axis, keepdims=True)
        # logmag = safe_div(mag-mag_min, mag_max-mag_min)

        # logmag = tf.math.log(logmag + EPSILON)
        # logmag1 = logmag * -1.
        # logmag2 = logmag + 18.420680743952367
        # logmag3 = tf.abs(tf.abs(logmag + logmag2) - 18.420680743952367)
        # comp = tf.concat([logmag1, logmag2, logmag3], axis=-1)

        # comp = tf.stack([complex_tensor[..., 0] / mag[..., 0] * logmag[..., 0],
        #                 complex_tensor[..., 1] / mag[..., 1] * logmag[..., 1],
        #                 complex_tensor[..., 2] / mag[..., 0] * logmag[..., 0],
        #                 complex_tensor[..., 3] / mag[..., 1] * logmag[..., 1]], axis=3)

        inputs = {
            'mel': mel,
            #'comp': comp
        }

        if y is None:
            return inputs
        return inputs, y
    return extract


def load_weights(model, filepath, custom=True):
    if not model._is_graph_network and not model.built:
        raise ValueError(
            'Unable to load weights saved in HDF5 format into a subclassed '
            'Model which has not created its variables yet. Call the Model '
            'first, then load the weights.')
    model._assert_weights_created()

    with h5py.File(filepath, 'r') as f:
        if 'layer_names' not in f.attrs and 'model_weights' in f:
            if not custom:
                f = f['model_weights']
                hdf5_format.load_weights_from_hdf5_group(f, model.layers)
            else:
                f = f['model_weights']['functional_1']

                layers = dict()
                for layer in model.layers:
                    weights = hdf5_format._legacy_weights(layer)
                    if weights:
                        layers[layer.name] = weights
                
                weight_value_tuples = []
                for l_name in f.keys():
                    assert l_name in layers.keys(), "The loaded weights (.h5) should have the same layers."

                    symbolic_weights = layers[l_name]

                    w_name = [x.name.split('/')[-1] for x in symbolic_weights]
                    weight_values = []
                    for n in w_name:
                        assert n in f[l_name], f'Layer: {l_name} should have the same kinds of weights between model and loaded weights.'
                        weight_values.append(f[l_name][n])

                    weight_value_tuples += zip(symbolic_weights, weight_values)
                K.batch_set_value(weight_value_tuples)


if __name__ == "__main__":
    config = args.parse_args()
    print(config)

    NAME = Path('logs', config.name)
    if config.test_swa:
        H5_NAME = NAME / (config.name + '_SWA.h5')
    else:
        H5_NAME = NAME / (config.name + '.h5')
    

    """ MODEL """
    input_mel = tf.keras.layers.Input(shape=(config.n_mels, config.n_frame, 6), name='mel')
    # input_comp = tf.keras.layers.Input(shape=(257, config.n_frame, 6), name='comp')
    
    # x = ZeroPadding2D((2, 3))(input_comp)
    # x = Conv2D(12, 7, strides=(2, 1), padding='valid', use_bias=False)(x)
    # x = BatchNormalization(axis=-1, momentum=0.99)(x)
    # x = tf.keras.layers.Activation('swish')(x)
    # x = Concatenate(axis=-1)([input_mel, x])
    x = input_mel
    model = getattr(model, config.model)(
        include_top=False,
        weights=None,
        input_tensor=x,
        backend=tf.keras.backend,
        layers=tf.keras.layers,
        models=tf.keras.models,
        utils=tf.keras.utils,
    )
    out = tf.transpose(model.output, perm=[0, 2, 1, 3])
    out = tf.keras.layers.Reshape([-1, out.shape[-1]*out.shape[-2]])(out)
    #out = Dropout(0.2)(out)

    #out = tf.keras.layers.Dense(256, activation='swish')(out)

    if config.n_layers > 0:
        if config.mode == 'GRU':
            out = tf.keras.layers.Dense(config.n_dim)(out)
            for i in range(config.n_layers):
                # out = transformer_layer(config.n_dim, config.n_heads)(out)
                out = tf.keras.layers.Bidirectional(
                    tf.keras.layers.GRU(config.n_dim, return_sequences=True),
                    backward_layer=tf.keras.layers.GRU(config.n_dim, 
                                                       return_sequences=True,
                                                       go_backwards=True))(out)
        elif config.mode == 'transformer':
            out = tf.keras.layers.Dense(config.n_dim)(out)
            out = encoder(config.n_layers,
                          config.n_dim,
                          config.n_heads)(out)

            out = tf.keras.layers.Flatten()(out)
            out = tf.keras.layers.ReLU()(out)

    out = tf.keras.layers.Dense(config.n_classes, activation='relu')(out)
    model = tf.keras.models.Model(inputs=model.input, outputs=out)
    
    #model.load_weights(str(H5_NAME))
    load_weights(model, str(H5_NAME), custom=True)
    print('loaded pretrained model')

    """ DATA """
    # wavs = glob.glob('/codes/2020_track3/t3_audio/*.wav')
    wavs = glob.glob('./data/t3_audio/*.wav')
    wavs.sort()
    to_mel = magphase_to_mel(config.n_mels)
    
    gt_angle = [[0, 0, 1, 0, 0, 1, 0, 0, 0, 0],
                [0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
                [0, 0, 0, 0, 0, 0, 0, 0, 2, 0],
                [0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
                [0, 0, 0, 0, 1, 0, 0, 1, 1, 0],
                [2, 0, 0, 1, 0, 0, 0, 0, 0, 0],
                [0, 0, 0, 0, 1, 0, 0, 0, 1, 1],
                [1, 2, 0, 0, 1, 0, 0, 1, 0, 0],
                [0, 1, 1, 1, 0, 0, 0, 0, 0, 0],
                [0, 0, 0, 1, 1, 0, 1, 1, 0, 0]]
    gt_class = [[1, 1, 0],
                [0, 0, 1],
                [1, 0, 1],
                [0, 1, 0],
                [0, 0, 3],
                [0, 1, 2],
                [2, 1, 0],
                [2, 2, 1],
                [0, 1, 2],
                [2, 1, 1]]
    
    wavs = list(map(load_wav, wavs)) 
    target = max([tuple(wav.shape) for wav in wavs]) 
    wavs = list(map(lambda x: tf.pad(x, [[0, 0], [0, target[1]-x.shape[1]], [0, 0]]), 
                    wavs)) 
    wavs = tf.convert_to_tensor(wavs) 
    #wavs = complex_to_magphase(wavs) 
    #wavs = magphase_to_mel(config.n_mels)(wavs) 
    #wavs = minmax_log_on_mel(wavs) 
    wavs = make_extractor(config)(wavs)
    wavs = model.predict(wavs)

    # wavs = list(map(load_wav, wavs))
    # wavs = list(map(complex_to_magphase, wavs))
    # wavs = list(map(to_mel, wavs))
    # wavs = list(map(minmax_log_on_mel, wavs))
    # target = max([tuple(wav.shape) for wav in wavs])
    # wavs = list(map(lambda x: tf.pad(x, [[0, 0], [0, target[1]-x.shape[1]], [0, 0]]),
    #                 wavs))
    # wavs = tf.convert_to_tensor(wavs)
    # wavs = model.predict(wavs) 

    wavs = wavs / config.multiplier
    wavs = tf.reshape(wavs, [*wavs.shape[:2], 3, 10])

    angles = tf.round(tf.reduce_sum(wavs, axis=(1, 2)))
    classes = tf.round(tf.reduce_sum(wavs, axis=(1, 3)))

    d_dir = D_direction(tf.cast(gt_angle, tf.float32), 
                        tf.cast(angles, tf.float32))
    d_cls = D_class(tf.cast(gt_class, tf.float32),
                    tf.cast(classes, tf.float32))

    d_total = (d_dir * 0.8 + d_cls * 0.2).numpy()
    print('total')
    print(d_total, d_total.mean())

    for i in range(len(gt_angle)):
        # plt.imshow(wav); plt.show()
        print(angles[i].numpy(), classes[i].numpy())
        print(gt_angle[i], gt_class[i])
        print()
