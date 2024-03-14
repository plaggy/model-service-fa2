import torch
import numpy as np
from torchaudio import functional as F
from transformers.pipelines.audio_utils import ffmpeg_read
from fastapi import HTTPException
import sys

# Code from insanely-fast-whisper:
# https://github.com/Vaibhavs10/insanely-fast-whisper

import logging
logger = logging.getLogger(__name__)

def preprocess_inputs(inputs, sampling_rate):
    inputs = ffmpeg_read(inputs, sampling_rate)

    if sampling_rate != 16000:
        inputs = F.resample(
            torch.from_numpy(inputs), sampling_rate, 16000
        ).numpy()

    if len(inputs.shape) != 1:
        logger.error(f"Diarization pipeline expecs single channel audio, received {inputs.shape}")
        raise HTTPException(
            status_code=400,
            detail=f"Diarization pipeline expecs single channel audio, received {inputs.shape}"
        )

    # diarization model expects float32 torch tensor of shape `(channels, seq_len)`
    diarizer_inputs = torch.from_numpy(inputs).float()
    diarizer_inputs = diarizer_inputs.unsqueeze(0)

    return inputs, diarizer_inputs


def diarize_audio(diarizer_inputs, diarization_pipeline, parameters):
    diarization = diarization_pipeline(
        {"waveform": diarizer_inputs, "sample_rate": parameters.sampling_rate},
        num_speakers=parameters.num_speakers,
        min_speakers=parameters.min_speakers,
        max_speakers=parameters.max_speakers,
    )

    segments = []
    for segment, track, label in diarization.itertracks(yield_label=True):
        segments.append(
            {
                "segment": {"start": segment.start, "end": segment.end},
                "track": track,
                "label": label,
            }
        )

    # diarizer output may contain consecutive segments from the same speaker (e.g. {(0 -> 1, speaker_1), (1 -> 1.5, speaker_1), ...})
    # we combine these segments to give overall timestamps for each speaker's turn (e.g. {(0 -> 1.5, speaker_1), ...})
    new_segments = []
    prev_segment = cur_segment = segments[0]

    for i in range(1, len(segments)):
        cur_segment = segments[i]

        # check if we have changed speaker ("label")
        if cur_segment["label"] != prev_segment["label"] and i < len(segments):
            # add the start/end times for the super-segment to the new list
            new_segments.append(
                {
                    "segment": {
                        "start": prev_segment["segment"]["start"],
                        "end": cur_segment["segment"]["start"],
                    },
                    "speaker": prev_segment["label"],
                }
            )
            prev_segment = segments[i]

    # add the last segment(s) if there was no speaker change
    new_segments.append(
        {
            "segment": {
                "start": prev_segment["segment"]["start"],
                "end": cur_segment["segment"]["end"],
            },
            "speaker": prev_segment["label"],
        }
    )

    return new_segments


def post_process_segments_and_transcripts(new_segments, transcript, group_by_speaker) -> list:
    # get the end timestamps for each chunk from the ASR output
    end_timestamps = np.array(
        [chunk["timestamp"][-1] if chunk["timestamp"][-1] is not None else sys.float_info.max for chunk in transcript])
    segmented_preds = []

    # align the diarizer timestamps and the ASR timestamps
    for segment in new_segments:
        # get the diarizer end timestamp
        end_time = segment["segment"]["end"]
        # find the ASR end timestamp that is closest to the diarizer's end timestamp and cut the transcript to here
        upto_idx = np.argmin(np.abs(end_timestamps - end_time))

        if group_by_speaker:
            segmented_preds.append(
                {
                    "speaker": segment["speaker"],
                    "text": "".join(
                        [chunk["text"] for chunk in transcript[: upto_idx + 1]]
                    ),
                    "timestamp": (
                        transcript[0]["timestamp"][0],
                        transcript[upto_idx]["timestamp"][1],
                    ),
                }
            )
        else:
            for i in range(upto_idx + 1):
                segmented_preds.append({"speaker": segment["speaker"], **transcript[i]})

        # crop the transcripts and timestamp lists according to the latest timestamp (for faster argmin)
        transcript = transcript[upto_idx + 1:]
        end_timestamps = end_timestamps[upto_idx + 1:]

        if len(end_timestamps) == 0:
            break

    return segmented_preds


def diarize(diarization_pipeline, file, parameters, asr_outputs):
    _, diarizer_inputs = preprocess_inputs(file, parameters.sampling_rate)

    segments = diarize_audio(
        diarizer_inputs, 
        diarization_pipeline, 
        parameters
    )

    return post_process_segments_and_transcripts(
        segments, asr_outputs["chunks"], group_by_speaker=False
    )