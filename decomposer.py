#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import os
import sys
from functools import partial
from glob import glob

import librosa
import numpy as np
from PIL import Image, ImageDraw
from moviepy.editor import AudioFileClip, VideoFileClip, ImageSequenceClip
from scipy.signal import find_peaks
from tqdm import tqdm

from signal_process_utils import generate_frequency_table, get_memory_usage

# logger with special stream handling to output to stdout in Node.js
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
stdout_handler.setLevel(logging.INFO)
# logger.addHandler(stdout_handler)


class Decomposer(object):

    def __init__(self, wav_file=None, stop_time=None, scale=2):
        """ Class to decompose an wav file into its frequency vs. time spectrogram,
        and map that to piano keys.

        Args:
            wav_file (str): name of wav file to process.
            stop_time (float): end time to trim song to
            scale (int): factor to resize origin image (from 1920x1080 resolution)
        """
        self.wav_file = wav_file
        self.stop_time = stop_time

        # init a fresh piano img (use HSV if not using addWeighted func in _generate_keyboard, else RGB)
        piano_img = os.path.join('assets', 'piano.jpg')
        self.piano_template = Image.open(piano_img).convert('RGBA')

        # downsize for memory optimization
        self.piano_template = self.piano_template.resize(
            tuple(x//scale for x in self.piano_template.size),
            Image.ANTIALIAS
        )

        # hardcoded constants
        self.max_freq = 4186        # Hz of high c (key 88). Sample rate is double (Nyquist Sampling Theorem).
        self.last_key_num = 89      # using a standard 88 key piano
        self.fps_out = 30           # fps of output video
        self.n_fft = 2048           # FFT window size for STFT spectrogram
        self.norm_algo = 'div_max'  # algorithm to normalize spectral vectors
        self.amp_thresh = 0.3       # float [0, 1] threshold normalized amplitudes must exceed to be mapped to piano

        # output image sizes
        self.length_full = self.piano_template.size[0]      # length of a full frame
        self.keyboard_width = self.piano_template.size[1]   # size of keyboard in video
        self.width_full = self.length_full*9//16             # expected width of a full frame (16:9 aspect ratio)

        # raw audio/acoustic data
        self.audio_ts, self.sample_rate = librosa.load(wav_file, sr=self.max_freq * 2, duration=self.stop_time)
        self.duration = librosa.get_duration(self.audio_ts, sr=self.sample_rate)
        self.freq_table = generate_frequency_table(scale=scale)

        def _find_nearest(value, array):
            """ Quantize a value (detected frequency) to piano's nearest fundemental frequency."""
            idx = np.argmin(np.abs(array - value))
            return self.last_key_num - idx

        self._map_freq2note = np.vectorize(partial(_find_nearest, array=self.freq_table['Frequency (Hz)'].values))

    def cvt_audio_to_piano(self):
        """ Apply the audio file to visual piano representation pipeline. """

        logger.info(f'[DECOMPOSER] >>>> Beginning pipeline. MEM: {get_memory_usage()}')
        self._generate_spectrogram()
        logger.info(f'[DECOMPOSER] >>>> _generate_spectrogram. MEM: {get_memory_usage()}')
        self._select_spectrogram()
        logger.info(f'[DECOMPOSER] >>>> _select_spectrogram. MEM: {get_memory_usage()}')
        self._parse_spectrogram()
        logger.info(f'[DECOMPOSER] >>>> _parse_spectrogram. MEM: {get_memory_usage()}')
        self._build_movie()
        logger.info(f'[DECOMPOSER] >>>> Built movie. Pipeline completed! MEM: {get_memory_usage()}')

    @staticmethod
    def _normalize_filter(matrix, axis=0, algo='div_max'):
        """ Normalize matrix along a given axis.

        Args:
            matrix (np.ndarray): matrix to normalize
            axis (int): {0 or 1} Default: 0. Axis to normalize along.
            algo (str): {div_max or zero_one}. Default div_max.
                div_max: divide all values by max in vector:x/max(x)
                zero_one: scale vector between [0-1]: (x - min(x)) / (max(x) - min(x))
        """
        norm_algo = {
            'div_max': lambda x: x / max(x),
            'zero_one': lambda x: (x - min(x)) / (max(x) - min(x))
        }
        normalized = np.apply_along_axis(
            norm_algo[algo],
            axis=axis,
            arr=matrix
        )
        normalized[np.isnan(normalized)] = 0
        return normalized

    @staticmethod
    def _median_filter(arr, length=5, stride=1):
        """ Compute the 1D median filter of an array. This helps remove outliers and noise.

        Args:
            arr (np.ndarray): arr to filter
            length (int): window size
            stride (int): step size

        Returns:
            smoothed np.ndarray
        """
        nrows = ((arr.size - length) // stride) + 1
        n = arr.strides[0]
        windowed_matrix = np.lib.stride_tricks.as_strided(arr, shape=(nrows, length), strides=(stride * n, n))
        median = np.median(windowed_matrix, axis=1)
        arr[-median.shape[0]:] = median
        return arr

    def _generate_spectrogram(self):
        """ Generate & filter spectrogram, generate corresponding time and frequency alignment vectors.
        Spectrogram generated by librosa's STFT using {self.n_fft} FFT window size, custom median
        filter is applied along the time axis.
        Performs Harmonic-Percussive Source Separation (HPSS) on this filtered STFT.
        Performs Vocal Separation on this HPSS filtered STFT. """

        self.spec_raw, phase = librosa.magphase(librosa.stft(self.audio_ts, self.n_fft))
        self.times = np.linspace(0, self.duration, self.spec_raw.shape[1])
        self.freqs = librosa.fft_frequencies(sr=self.sample_rate, n_fft=self.n_fft)

        logger.info('[DECOMPOSER] >>>> Generated raw spectrogram.')

        if self.stop_time:
            self.t_final = np.where(self.times < self.stop_time)[0][-1]
        else:
            self.t_final = self.times.shape[0]

        # median filter along time axis to get rid of white noise
        self.spec_raw = np.apply_along_axis(self._median_filter, 1, self.spec_raw)

        # apply additonal spectrogram maniplation
        self.spec_harmonic, self.spec_percussive = librosa.decompose.hpss(self.spec_raw, margin=2)
        self.spec_foreground, self.spec_background = self._spectrogram_separate_vocals(self.spec_harmonic)

        logger.info('[DECOMPOSER] >>>> Perfomed HPSS and Vocal Separation.')

    def _spectrogram_separate_vocals(self, spectrogram):
        """ Use Librosa's nearest-neighbor-filtering to separate voice from background of spectrogram.

        Args:
            spectrogram (np.ndarray): spectrogram to process.

        Returns:
            np.ndarray: spectrogram of foreground (voice)
            np.ndarray: spectrogram of background (harmonics)

        """
        s_filter = librosa.decompose.nn_filter(
            spectrogram,
            aggregate=np.median,
            metric='cosine',
            width=int(librosa.time_to_frames(2, sr=self.sample_rate))
        )

        s_filter = np.minimum(spectrogram, s_filter)
        margin_i, margin_v, power = 2, 10, 2

        mask_i = librosa.util.softmask(
            s_filter,
            margin_i * (spectrogram - s_filter),
            power=power
        )

        mask_v = librosa.util.softmask(
            spectrogram - s_filter,
            margin_v * s_filter,
            power=power
        )

        s_foreground = mask_v * spectrogram
        s_background = mask_i * spectrogram

        logger.info(f'[DECOMPOSER] >>>> Separated vocals from spectrogram.')

        return s_foreground, s_background

    def _select_spectrogram(self, spec_type='harmonic'):
        """ Select type of spectrogram to use (raw, harmonic or percussive - generated by HPSS).

        Args:
            spec_type (str): {raw, harmonic, or percussive}. Default: 'raw'.
                Type of spectrogram to use in downstream parsing.
        """
        spec_selector = {
            'raw': self.spec_raw,
            'harmonic': self.spec_harmonic,
            'percussive': self.spec_percussive,
            'foreground': self.spec_foreground,
            'background': self.spec_background,
        }
        self.amplitudes = spec_selector[spec_type]

        logger.info(f'[DECOMPOSER] >>>> Selected spectrogram type: {spec_type}.')

    def _parse_spectrogram(self):
        """ Parse the spectrogram by iterating through the time axis, thresholding
        away quiet frequencies, and mapping the dominant frequencies to piano keys."""

        def _get_peaks_and_threshold(t):
            """ Given a time(t), extract the dominant frequencies in the amplitude
            matrix using argrelextrema, and threshold all other values to zero.
            Store in a new matrix, self.dominant_amplitudes. Thresholding performed by
            selecting the inverse indices of the detected peaks. Populates class
            property matrcies.

            Args:
                t (int): time point to extract note data

            """

            # peak detection in a amplitude vector at time t
            # take log of vec since amplitudes decay exponentially at higher freqs
            # https://stackoverflow.com/questions/1713335/peak-finding-algorithm-for-python-scipy/52612432#52612432
            peaks_idx = find_peaks(np.log(self.amplitudes[:, t]), prominence=3)[0]

            # select inverse indices of peaks, threshold to zero
            arr = self.dominant_amplitudes[:, t]
            ia = np.indices(arr.shape)
            not_indices = np.setxor1d(ia, peaks_idx)
            arr[not_indices] = 0
            self.dominant_amplitudes[:, t] = arr

        def _get_notes(t):
            """ Map the dominant frequencies at time(t) to the corresponding piano keys.

            If we detect a frequency in self.dominant_amplitudes, quantize that frequency into
            one of the piano frequency bins, store in array detected_freqs, and get the piano note
            index using self._map_freq2note.

            Args:
                t (int): time point to extract note data
            Returns
                np.ndarray or None: array of key numbers active at time (t)
                np.ndarray or None: array of corresponding ampltidues active at time (t)
            """

            # if dominant frequency vector is non-zero, map all detected freqs to notes
            amp_arr = self.dominant_amplitudes[:, t]
            if np.count_nonzero(amp_arr) != 0:
                freq_idx_non_zero = np.nonzero(amp_arr)[0]
                detected_freqs = self.freqs[freq_idx_non_zero]

                # active key numbers and corresponding amplitudes
                key_number_array = self._map_freq2note(detected_freqs)
                amp_array_non_zero = amp_arr[freq_idx_non_zero]

                # Note: Chromagram uses raw amplitude values. It has not been normalzied or thresholded!
                self.chromagram[key_number_array-1, t] = amp_array_non_zero
                return key_number_array, amp_array_non_zero
            return None, None

        # sizes and variables for genreating full frame visualization
        num_time_steps_in_1_sec = int(self.t_final / (self.stop_time or self.duration))
        stretch_vec_factor = int((self.width_full - self.keyboard_width) / num_time_steps_in_1_sec)
        piano_roll_width = num_time_steps_in_1_sec * stretch_vec_factor
        self.width_full = self.keyboard_width + piano_roll_width  # update full frame size to approximated

        # init matrices for creating video from frames.
        # tmp_frame for writing smaller videos, avoids full video frame matrix in memory
        piano_roll_size = [self.t_final + num_time_steps_in_1_sec, 1, self.length_full, 3]
        full_frame_size = [self.t_final, self.width_full, self.length_full, 3]
        tmp_frame_size = [self.width_full, self.length_full, 3]

        piano_roll = np.empty(piano_roll_size, dtype=np.uint8)
        full_frame_buffer = np.empty(full_frame_size, dtype=np.uint8)
        tmp_frame = np.empty(tmp_frame_size, dtype=np.uint8)

        # init dom freqs matrix, iterate through time, find peaks and threshold
        self.dominant_amplitudes = self.amplitudes.copy()
        for time in tqdm(range(self.t_final)):
            _get_peaks_and_threshold(time)
        logger.info(f'[DECOMPOSER] >>>> Parsed spectrogram. Found dominant frequencies. MEM {get_memory_usage()}')

        # median filter along time axis to get rid of white noise
        self.dominant_amplitudes = np.apply_along_axis(self._median_filter, 1, self.dominant_amplitudes)

        # iterate through time, map dominant frequencies to notes, generate keyboard visualizations
        self.chromagram = np.zeros((self.last_key_num, self.t_final))
        for time in tqdm(range(self.t_final)):
            active_keys, active_ampltidues = _get_notes(time)
            keyboard, piano_roll_slice = self._generate_keyboard(active_keys, active_ampltidues)
            full_frame_buffer[time, piano_roll_width:, ...] = keyboard
            piano_roll[time, ...] = piano_roll_slice
        logger.info(
            f'[DECOMPOSER 1/2] >>>> Mapped frequencies to notes and generated keyboard visualizations. '
            f'MEM: {get_memory_usage()}'
        )

        for time in tqdm(range(self.t_final)):
            roll_slice = np.flip(np.squeeze(piano_roll[time:time + num_time_steps_in_1_sec, :]), axis=0)
            p_frame = np.repeat(roll_slice, repeats=stretch_vec_factor, axis=0)
            keyboard = full_frame_buffer[time, piano_roll_width:, ...]
            tmp_frame[:piano_roll_width, ...] = p_frame
            tmp_frame[piano_roll_width:, ...] = keyboard
            np.save(f'img/{time}.npy', tmp_frame)

        logger.info(
            f'[DECOMPOSER 2/2] >>>> Mapped frequencies to notes and generated keyboard visualizations. '
            f'MEM: {get_memory_usage()}'
        )

    def _generate_keyboard(self, key_number_array, amp_array_non_zero):
        """ Iterate through notes found in sample and draw on keyboard image.
        Intensity of color depends on loudness (decibels).
        All detected notes are stacked into a single image.

        Args:
            key_number_array (np.ndarray): indices of active notes in self.freq_table.
            amp_array_non_zero (np.ndarray): vector containing raw amplitude values
        Returns:
            np.ndarray image of colorized piano

        """
        piano_out = self.piano_template.copy()
        piano_roll_slice = np.zeros((1, self.length_full, 3), dtype=np.uint8)
        if key_number_array is not None:
            amp_array_non_zero = self._normalize_filter(amp_array_non_zero, algo=self.norm_algo)

            # iterate through detected notes, extract location on keyboard if loudness thresh met
            for n in range(key_number_array.shape[0]):
                idx = key_number_array[n]
                loudness = amp_array_non_zero[n]

                if loudness > self.amp_thresh:
                    piano_loc_points = self.freq_table.iat[self.last_key_num - idx, -1]
                    if type(piano_loc_points) is not list:
                        continue  # handle nan case

                    # fill in time vector for piano roll
                    piano_roll_slice[:, piano_loc_points[0][0]:piano_loc_points[-1][0], 1] = int(255 * loudness)

                    # color in detected note on keyboard img, stack onto output img
                    poly = Image.new('RGBA', (self.length_full, self.keyboard_width))
                    pdraw = ImageDraw.Draw(poly)
                    pdraw.polygon(piano_loc_points, fill=(0, 255, 0, int(255 * loudness)), outline=(0, 255, 240, 255))
                    piano_out.paste(poly, mask=poly)
        return np.array(piano_out.convert('RGB')), piano_roll_slice

    def _format_chromagram(self, thresh=None):
        """
        Args:
            thresh (float): [0, 1] threshold normalized amplitudes must exceed to be mapped to piano.
                See Also:  self.amp_thresh

        Returns:
            np.ndarray: normalized, filtered chromagram  (88 key-y-axis).

        """
        chromagram = self._normalize_filter(self.chromagram, algo=self.norm_algo)
        chromagram[chromagram < (thresh or self.amp_thresh)] = 0
        return chromagram

    def _plot_spectrogram(self, spectrogram, title='', scaler='db', **kwargs):
        """ Plot spectrograms for debugging.

        Args:
            spectrogram (np.ndarray): fourier spectrogram
            title (str): name of spectrogram
            scaler (str or None): 'db'or 'log'. Ampltude scaling to decibel or log10
            y_axis (str or None): 'log', or 'linear'

        Optional kwargs (passed onto librosa.display.specshow)
            y_axis : None or str
                Range for the x- and y-axes.
                Frequency types:

                - 'linear', 'fft', 'hz' : frequency range is determined by the FFT window and sampling rate.
                - 'log' : the spectrum is displayed on a log scale.
                - 'mel' : frequencies are determined by the mel scale.
                - 'cqt_hz' : frequencies are determined by the CQT scale.
                - 'cqt_note' : pitches are determined by the CQT scale.

            x_coords : np.ndarray [shape=data.shape[1]+1]
            y_coords : np.ndarray [shape=data.shape[0]+1]

        """
        if scaler not in ['db', 'log', 'linear', 'mel', 'chromagram']:
            raise ValueError('Given scaler argument is not valid.')

        import matplotlib.pyplot as plt
        import librosa.display

        plt.figure(figsize=(20, 6))

        # choose visualization scaling type
        toh = 10e-12  # sound intensity threshold of human hearing

        def _get_spec_scaler(_spectrogram, _scaler):
            if _scaler == 'db':
                return {'data': librosa.amplitude_to_db(spectrogram, ref=toh), 'y_axis': 'log'}
            if _scaler == 'log':
                return {'data': 10 * np.log10(_spectrogram + 1e-9), 'y_axis': 'log'}  # manual log scaling
            if _scaler == 'linear':
                return {'data': _spectrogram, 'y_axis': 'linear'}
            if _scaler == 'mel':
                return {'data': _spectrogram, 'y_axis': 'mel'}
            if _scaler == 'chromagram':
                return {'data': self._format_chromagram(), 'y_axis': 'linear'}

        librosa.display.specshow(
            **_get_spec_scaler(spectrogram, scaler), **kwargs, x_axis='time', fmax=self.max_freq, sr=self.sample_rate
        )

        plt.title(title)
        plt.colorbar(format='%+2.0f dB')
        plt.tight_layout()
        plt.show()

    def _build_movie(self):
        """ Concatenate self._full_frames images into video file, add back original music. """
        outname = self.wav_file.replace('input', 'output')
        outname = outname.replace('wav', 'mp4')
        npy_files = sorted(glob('img/*npy'), key=lambda x: int(x[4:-4]))  # sort by num

        # import cv2
        # fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        # out = cv2.VideoWriter(outname, fourcc, 20.0, (640, 480))
        # [out.write(np.load(frame)) for frame in npy_files]
        # out.release()
        # output = VideoFileClip(outname)

        output = ImageSequenceClip([np.load(t) for t in npy_files], fps=self.fps_out / 2)
        output = output.cutout(0, 1)  # trim to compensate for FFT lag
        output = output.set_audio(AudioFileClip(self.wav_file))
        output.write_videofile(
            outname,
            fps=self.fps_out,
            temp_audiofile="temp-audio.m4a",
            remove_temp=True,
            codec="libx264",
            audio_codec="aac"
        )
        [os.remove(npy_file) for npy_file in npy_files]
