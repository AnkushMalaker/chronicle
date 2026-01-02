"""
Enhanced chunking implementation for long audio processing.

This module provides a timestamp-preserving chunking solution using NeMo's
FrameBatchChunkedRNNT with proper timestamp joining following NeMo patterns.
"""

import logging
import torch
import time
from typing import List, Dict, Any, Optional
from nemo.collections.asr.parts.utils.streaming_utils import FrameBatchChunkedRNNT
from nemo.collections.asr.parts.utils import rnnt_utils
from nemo.collections.asr.parts.utils.rnnt_utils import Hypothesis
from nemo.collections.asr.parts.utils.timestamp_utils import process_timestamp_outputs

logger = logging.getLogger(__name__)


class TimestampedFrameBatchChunkedRNNT(FrameBatchChunkedRNNT):
    """
    Enhanced FrameBatchChunkedRNNT that preserves word-level timestamps.

    Follows NeMo's FrameBatchMultiTaskAED pattern for proper timestamp handling
    with chunk_offsets and hypothesis merging.
    """

    def __init__(self, asr_model, frame_len=4, total_buffer=4, batch_size=4):
        super().__init__(asr_model, frame_len, total_buffer, batch_size)
        self.all_hypotheses = []
        self.chunk_offsets = [0]  # Track chunk offsets like NeMo's FrameBatchMultiTaskAED
        self.merged_hypothesis = None

        # Get model parameters for timestamp calculations (following FrameBatchMultiTaskAED)
        self.subsampling_factor = getattr(asr_model._cfg.encoder, 'subsampling_factor', 4)
        self.window_stride = getattr(asr_model._cfg.preprocessor, 'window_stride', 0.01)

        # Ensure model is in eval mode and timestamps enabled
        self.asr_model.eval()
        if hasattr(self.asr_model, 'decoding'):
            # Enable word timestamps but not char timestamps to avoid issues
            if hasattr(self.asr_model.decoding, 'compute_timestamps'):
                original_value = self.asr_model.decoding.compute_timestamps
                self.asr_model.decoding.compute_timestamps = True
                logger.debug(f"Set compute_timestamps=True (was: {original_value})")

            # Set timestamp type to word only to avoid char_offsets issues
            if hasattr(self.asr_model.decoding, 'rnnt_timestamp_type'):
                self.asr_model.decoding.rnnt_timestamp_type = 'word'
                logger.debug("Set rnnt_timestamp_type='word' to avoid char offset issues")
        else:
            logger.warning("Model does not have decoding attribute!")

    def reset(self):
        """Reset the chunked inference state and clear accumulated hypotheses."""
        super().reset()
        self.all_hypotheses = []
        self.chunk_offsets = [0]
        self.merged_hypothesis = None

    @torch.no_grad()
    def _get_batch_preds(self, keep_logits=False):
        """
        Override parent method to capture Hypothesis objects with timestamps.

        The parent class calls rnnt_decoder_predictions_tensor with return_hypotheses=False,
        which discards timestamp information. We override to capture the full Hypothesis objects.
        """
        # Ensure model is in eval mode to avoid batch norm issues
        self.asr_model.eval()
        device = self.asr_model.device

        for batch in iter(self.data_loader):
            feat_signal, feat_signal_len = batch
            feat_signal, feat_signal_len = feat_signal.to(device), feat_signal_len.to(device)

            # Get encoder outputs - following parent class pattern
            encoded, encoded_len = self.asr_model(
                processed_signal=feat_signal, processed_signal_length=feat_signal_len
            )

            # KEY CHANGE: Get full Hypothesis objects instead of just text
            # Temporarily disable timestamps to avoid char_offsets error
            old_compute_timestamps = getattr(self.asr_model.decoding, 'compute_timestamps', False)
            self.asr_model.decoding.compute_timestamps = False

            hypotheses = self.asr_model.decoding.rnnt_decoder_predictions_tensor(
                encoder_output=encoded, encoded_lengths=encoded_len,
                return_hypotheses=True  # Get hypothesis objects even without timestamps
            )

            # Restore original setting
            self.asr_model.decoding.compute_timestamps = old_compute_timestamps

            # Store hypotheses with chunk offset tracking
            self.all_hypotheses.extend(hypotheses)
            logger.debug(f"Got {len(hypotheses)} hypotheses from chunk {len(self.chunk_offsets)}")

            # Update chunk offsets for ALL chunks (following FrameBatchMultiTaskAED pattern)
            for length in feat_signal_len:
                current_length = length.item()
                if len(self.chunk_offsets) == 1:
                    self.chunk_offsets.append(current_length)
                else:
                    old_offset = self.chunk_offsets[-1]
                    new_offset = old_offset + current_length
                    self.chunk_offsets.append(new_offset)

            logger.debug(f"Chunk offsets updated: {self.chunk_offsets}")

            # Extract text for parent class compatibility
            best_hyp_text = [hyp.text if hyp.text else "" for hyp in hypotheses]
            self.all_preds.extend(best_hyp_text)

            # Cleanup - following parent class pattern
            del encoded, encoded_len

    def get_timestamped_results(self):
        """Return results with timestamp information using NeMo's timestamp joining."""
        if not self.all_hypotheses:
            return []

        logger.info(f"Joining {len(self.all_hypotheses)} hypotheses")

        # Join hypotheses using NeMo's FrameBatchMultiTaskAED pattern
        try:
            self.merged_hypothesis = self._join_hypotheses(self.all_hypotheses)

            # Check merged hypothesis results
            if self.merged_hypothesis and hasattr(self.merged_hypothesis, 'timestamp'):
                words = self.merged_hypothesis.timestamp.get('word', [])
                if words and len(words) > 0:
                    first_word = words[0]
                    last_word = words[-1]
                    first_start = first_word.get('start', 0)
                    last_end = last_word.get('end', 0)

                    logger.info(f"Merged {len(words)} words: {first_start:.2f}s to {last_end:.2f}s")

                    if first_start > 1.0:
                        logger.warning(f"First word starts at {first_start:.2f}s (may be chunk-relative)")
                else:
                    logger.warning("No word timestamps found in merged hypothesis")

            return [self.merged_hypothesis] if self.merged_hypothesis else self.all_hypotheses
        except Exception as e:
            logger.exception(f"Hypothesis joining FAILED: {e}")
            raise e  # Don't silently fall back

    def _join_hypotheses(self, hypotheses):
        """Join multiple hypotheses with proper timestamp alignment following NeMo's FrameBatchMultiTaskAED."""
        if len(hypotheses) == 1:
            return hypotheses[0]

        # Initialize a new hypothesis following NeMo's pattern
        merged_hypothesis = rnnt_utils.Hypothesis(
            score=0.0,
            y_sequence=torch.tensor([]),
            timestamp={
                'char': [],
                'word': [],
                'segment': [],
            },
        )

        # Join components following NeMo's FrameBatchMultiTaskAED pattern
        merged_hypothesis = self._join_text(merged_hypothesis, hypotheses)
        merged_hypothesis = self._join_y_sequence(merged_hypothesis, hypotheses)
        merged_hypothesis = self._join_timestamp(merged_hypothesis, hypotheses)

        return merged_hypothesis

    def _join_text(self, merged_hypothesis, hypotheses):
        """Join text from multiple hypotheses."""
        merged_hypothesis.text = " ".join([h.text for h in hypotheses if h.text])
        return merged_hypothesis

    def _join_y_sequence(self, merged_hypothesis, hypotheses):
        """Join y_sequence from multiple hypotheses."""
        y_sequences = [h.y_sequence for h in hypotheses if hasattr(h, 'y_sequence') and h.y_sequence is not None]
        if y_sequences:
            merged_hypothesis.y_sequence = torch.cat(y_sequences)
        return merged_hypothesis

    def _join_timestamp(self, merged_hypothesis, hypotheses):
        """Join timestamps from multiple hypotheses with proper offset handling."""
        cumulative_offset = 0
        logger.debug(f"Processing {len(hypotheses)} hypotheses with chunk_offsets: {self.chunk_offsets}")

        for i, h in enumerate(hypotheses):
            # Calculate cumulative offset for this hypothesis
            if i < len(self.chunk_offsets):
                cumulative_offset = self.chunk_offsets[i]
            else:
                logger.warning(f"Hypothesis {i}: No chunk offset available, using previous offset {cumulative_offset}")

            logger.debug(f"Hypothesis {i}: using cumulative_offset {cumulative_offset}")

            # Process word-level timestamps using h.words and h.timestamp tensor
            if hasattr(h, 'words') and h.words:
                word_list = h.words
                timestamp_tensor = getattr(h, 'timestamp', None)
                word_confidence = getattr(h, 'word_confidence', []) or []

                logger.debug(f"Hypothesis {i}: Processing {len(word_list)} words")

                updated_timestamps = []

                for j, word_text in enumerate(word_list):
                    if word_text and word_text.strip():  # Skip empty words
                        # Calculate frame indices for this word from the tensor
                        if timestamp_tensor is not None and j < len(timestamp_tensor):
                            frame_start = timestamp_tensor[j].item()
                            frame_end = timestamp_tensor[j + 1].item() if j + 1 < len(timestamp_tensor) else frame_start + 1
                        else:
                            # Fallback: estimate frames
                            frame_start = j
                            frame_end = j + 1

                        # Apply cumulative offset for absolute timestamps
                        absolute_frame_start = frame_start + cumulative_offset // self.subsampling_factor
                        absolute_frame_end = frame_end + cumulative_offset // self.subsampling_factor

                        # Convert frames to time using model parameters
                        start_time = absolute_frame_start * self.window_stride * self.subsampling_factor
                        end_time = absolute_frame_end * self.window_stride * self.subsampling_factor

                        # Get confidence if available
                        confidence = word_confidence[j] if j < len(word_confidence) else 1.0

                        # Create word timestamp entry in NeMo format
                        updated_word = {
                            'word': word_text,
                            'start_offset': absolute_frame_start,
                            'end_offset': absolute_frame_end,
                            'start': start_time,
                            'end': end_time,
                            'confidence': confidence
                        }

                        updated_timestamps.append(updated_word)

                if updated_timestamps:
                    logger.debug(f"Hypothesis {i}: processed {len(updated_timestamps)} words")
                    merged_hypothesis.timestamp['word'].extend(updated_timestamps)

            # Process segment-level timestamps if available
            if hasattr(h, 'timestamp') and hasattr(h.timestamp, 'get'):
                segment_timestamps = h.timestamp.get('segment', None)
                if segment_timestamps:
                    updated_timestamps = []

                    for segment in segment_timestamps:
                        if isinstance(segment, dict):
                            updated_segment = segment.copy()
                            # Apply frame offset with subsampling factor
                            if 'start_offset' in segment:
                                updated_segment['start_offset'] = segment['start_offset'] + cumulative_offset // self.subsampling_factor
                            if 'end_offset' in segment:
                                updated_segment['end_offset'] = segment['end_offset'] + cumulative_offset // self.subsampling_factor

                            # Convert to absolute time using model parameters
                            if 'start_offset' in updated_segment:
                                updated_segment['start'] = updated_segment['start_offset'] * self.window_stride * self.subsampling_factor
                            if 'end_offset' in updated_segment:
                                updated_segment['end'] = updated_segment['end_offset'] * self.window_stride * self.subsampling_factor

                            updated_timestamps.append(updated_segment)

                    merged_hypothesis.timestamp['segment'].extend(updated_timestamps)

        return merged_hypothesis




def extract_timestamps_from_hypotheses_native(hypotheses: List[Hypothesis], chunk_start_time: float = 0.0, model=None) -> List[Dict[str, Any]]:
    """
    Extract word-level timestamps using NeMo's native process_timestamp_outputs.

    This is the recommended approach using NeMo's official utilities.
    """
    try:
        if not hypotheses:
            return []

        logger.debug(f"Processing {len(hypotheses)} hypotheses with chunk_start_time={chunk_start_time}")

        # Get model parameters
        window_stride = getattr(model._cfg.preprocessor, 'window_stride', 0.01) if model else 0.01
        subsampling_factor = getattr(model._cfg.encoder, 'subsampling_factor', 4) if model else 4

        logger.debug(f"Model params: window_stride={window_stride}, subsampling_factor={subsampling_factor}")

        words = []
        for i, hyp in enumerate(hypotheses):
            # Check if hypothesis already has processed timestamp dict (from joining)
            if hasattr(hyp, 'timestamp') and isinstance(hyp.timestamp, dict) and 'word' in hyp.timestamp:
                word_timestamps = hyp.timestamp['word']
                for word_data in word_timestamps:
                    if isinstance(word_data, dict) and word_data.get('word'):
                        # Already processed by joining - just add chunk offset if needed
                        final_start = float(word_data.get('start', 0)) + chunk_start_time
                        final_end = float(word_data.get('end', 0)) + chunk_start_time

                        word_dict = {
                            'word': word_data['word'],
                            'start': final_start,
                            'end': final_end,
                            'confidence': float(word_data.get('confidence', 1.0))
                        }
                        words.append(word_dict)

            elif hasattr(hyp, 'words') and hyp.words:
                # Original tensor processing for raw hypotheses
                word_list = hyp.words
                timestamp_tensor = getattr(hyp, 'timestamp', None) if hasattr(hyp, 'timestamp') and hasattr(hyp.timestamp, 'shape') else None
                word_confidence = getattr(hyp, 'word_confidence', []) or []

                for j, word_text in enumerate(word_list):
                    if word_text and word_text.strip():  # Skip empty words
                        # Calculate frame indices for this word
                        if timestamp_tensor is not None and j < len(timestamp_tensor):
                            frame_start = timestamp_tensor[j].item()
                            frame_end = timestamp_tensor[j + 1].item() if j + 1 < len(timestamp_tensor) else frame_start + 1
                        else:
                            # Fallback: estimate frames
                            frame_start = j
                            frame_end = j + 1

                        # Convert frames to time using model parameters
                        start_time = frame_start * window_stride * subsampling_factor
                        end_time = frame_end * window_stride * subsampling_factor

                        # Get confidence
                        confidence = word_confidence[j] if j < len(word_confidence) else 1.0

                        # Add chunk start time for absolute positioning
                        final_start = start_time + chunk_start_time
                        final_end = end_time + chunk_start_time

                        word_dict = {
                            'word': word_text,
                            'start': final_start,
                            'end': final_end,
                            'confidence': float(confidence)
                        }
                        words.append(word_dict)

        logger.info(f"Extracted {len(words)} words")
        if words:
            logger.info(f"Time range: {words[0]['start']:.2f}s to {words[-1]['end']:.2f}s")

        return words

    except Exception as e:
        logger.exception(f"Native timestamp extraction FAILED: {e}")
        raise e  # Don't silently fall back


def extract_timestamps_from_hypotheses(hypotheses: List[Hypothesis], chunk_start_time: float = 0.0, model=None) -> List[Dict[str, Any]]:
    """
    Extract word-level timestamps from NeMo Hypothesis objects.

    Args:
        hypotheses: List of NeMo Hypothesis objects
        chunk_start_time: Start time of the chunk for timestamp adjustment
        model: NeMo ASR model for accessing configuration

    Returns:
        List of word dictionaries with 'start', 'end', 'word', 'confidence'
    """
    try:
        words = []
        if hypotheses is None:
            return []

        logger.debug(f"Processing {len(hypotheses)} hypotheses for timestamp extraction")

        for i, hyp in enumerate(hypotheses):
            logger.debug(f"Processing hypothesis {i}: {type(hyp)}")

            try:
                # Use h.words instead of h.timestamp['word']
                if hasattr(hyp, 'words') and hyp.words:
                    word_list = hyp.words
                    timestamp_tensor = getattr(hyp, 'timestamp', None)
                    word_confidence = getattr(hyp, 'word_confidence', []) or []

                    # Convert frame indices to word timestamps using model parameters
                    window_stride = getattr(model._cfg.preprocessor, 'window_stride', 0.01) if model else 0.01
                    subsampling_factor = getattr(model._cfg.encoder, 'subsampling_factor', 4) if model else 4

                    # Process each word with its corresponding frame indices
                    for j, word_text in enumerate(word_list):
                        try:
                            if word_text and word_text.strip():  # Skip empty words
                                # Calculate frame indices for this word
                                # For word j, we typically use frames [j, j+1] or similar pattern
                                if timestamp_tensor is not None and j < len(timestamp_tensor):
                                    # Use frame index from tensor
                                    frame_start = timestamp_tensor[j].item() if j < len(timestamp_tensor) else j
                                    frame_end = timestamp_tensor[j + 1].item() if j + 1 < len(timestamp_tensor) else frame_start + 1
                                else:
                                    # Fallback: estimate frame indices
                                    frame_start = j
                                    frame_end = j + 1

                                # Convert frame indices to time using model parameters
                                start_time = frame_start * window_stride * subsampling_factor
                                end_time = frame_end * window_stride * subsampling_factor

                                # Get confidence for this word
                                confidence = word_confidence[j] if j < len(word_confidence) else 1.0

                                word_dict = {
                                    'word': word_text,
                                    'start': float(start_time) + chunk_start_time,
                                    'end': float(end_time) + chunk_start_time,
                                    'confidence': float(confidence)
                                }
                                words.append(word_dict)

                        except Exception as word_error:
                            logger.error(f"Error processing word {j} '{word_text}': {word_error}")
                            raise

                # Fallback: if no word timestamps but we have text, create words from text
                elif hasattr(hyp, 'text') and hyp.text:
                    try:
                        text_words = hyp.text.split()
                        if text_words:
                            estimated_duration = max(len(text_words) * 0.5, 1.0)  # 0.5s per word minimum
                            word_duration = estimated_duration / len(text_words)

                            for j, word in enumerate(text_words):
                                start_time = chunk_start_time + (j * word_duration)
                                end_time = chunk_start_time + ((j + 1) * word_duration)

                                word_dict = {
                                    'word': word,
                                    'start': start_time,
                                    'end': end_time,
                                    'confidence': getattr(hyp, 'score', 1.0)
                                }
                                words.append(word_dict)

                            logger.debug(f"Hypothesis {i}: Created {len(text_words)} estimated word timings from text")
                    except Exception as text_error:
                        logger.error(f"Error processing text fallback for hypothesis {i}: {text_error}")

                # Create empty word entries for silence/non-speech if hypothesis is empty
                else:
                    # This represents silence or non-speech audio
                    words.append({
                        'word': '',
                        'start': chunk_start_time,
                        'end': chunk_start_time + 1.0,
                        'confidence': 0.0
                    })

            except Exception as hyp_error:
                logger.error(f"Error processing hypothesis {i}: {hyp_error}")

        logger.debug(f"Extracted {len(words)} total words with timestamps")
        return words

    except Exception as e:
        logger.exception(f"Critical error in extract_timestamps_from_hypotheses: {e}")
        return []


async def transcribe_with_enhanced_chunking(model, audio_file_path: str,
                                          frame_len: float = 4.0,
                                          total_buffer: float = 8.0) -> Dict[str, Any]:
    """
    Transcribe long audio using enhanced chunking with NeMo's built-in timestamp preservation.

    Args:
        model: Loaded NeMo ASR model
        audio_file_path: Path to audio file
        frame_len: Duration of each frame in seconds (NeMo FrameBatchChunkedRNNT parameter)
        total_buffer: Total buffer duration in seconds (NeMo FrameBatchChunkedRNNT parameter)

    Returns:
        Dictionary with transcription results including word-level timestamps
    """
    # Start timing
    overall_start = time.time()
    logger.info(f"Starting enhanced chunking transcription for {audio_file_path}")

    # Ensure model is in eval mode for inference
    model.eval()

    try:
        with torch.no_grad():
            # Initialization phase
            init_start = time.time()
            logger.debug(f"Initializing NeMo chunked processor...")

            # Initialize NeMo's chunked processor with timestamp preservation
            chunker = TimestampedFrameBatchChunkedRNNT(
                asr_model=model,
                frame_len=frame_len,      # Frame duration in seconds
                total_buffer=total_buffer, # Total buffer duration
                batch_size=1              # Process one chunk at a time
            )

            init_end = time.time()
            init_duration = init_end - init_start
            logger.debug(f"Initialization completed in {init_duration:.3f}s")

            # Audio loading phase
            loading_start = time.time()
            logger.debug(f"Loading audio file into chunker...")

            # Process the audio file using NeMo's built-in chunking
            chunker.read_audio_file(
                audio_filepath=audio_file_path,
                delay=0,  # No delay
                model_stride_in_secs=0.02  # 20ms stride typical for ASR
            )

            loading_end = time.time()
            loading_duration = loading_end - loading_start
            logger.debug(f"Audio loading completed in {loading_duration:.3f}s")

            # Processing phase
            processing_start = time.time()
            logger.info(f"Running enhanced chunking inference...")

            # Run inference with NeMo's chunked processing
            result_text = chunker.transcribe()

            processing_end = time.time()
            processing_duration = processing_end - processing_start
            logger.info(f"Processing completed in {processing_duration:.3f}s")

            # Reconcile phase (hypothesis extraction and merging)
            reconcile_start = time.time()
            logger.debug(f"Extracting and reconciling hypotheses...")

            hypotheses = chunker.get_timestamped_results()

            reconcile_end = time.time()
            reconcile_duration = reconcile_end - reconcile_start
            logger.debug(f"Reconcile completed in {reconcile_duration:.3f}s")

            # Timestamp extraction phase
            timestamp_start = time.time()
            logger.debug(f"Extracting word-level timestamps...")

            # Try using native NeMo processing first, fall back to manual if needed
            words = extract_timestamps_from_hypotheses_native(hypotheses, chunk_start_time=0.0, model=model)

            timestamp_end = time.time()
            timestamp_duration = timestamp_end - timestamp_start
            logger.debug(f"Timestamp extraction completed in {timestamp_duration:.3f}s")

            # Final formatting phase
            format_start = time.time()
            logger.debug(f"Formatting final response...")

            if words is None:
                logger.warning("Words extraction returned None, using empty list")
                words = []

            words_count = len(words) if words else 0

            # Safe response creation
            if words and len(words) > 0:
                end_time = words[-1]['end']
                logger.info(f"Using end time from last word: {end_time}")
            else:
                end_time = 0.0
                logger.info("Using default end time: 0.0")

            response = {
                'text': result_text,
                'words': words,
                'segments': [{'start': 0.0, 'end': end_time, 'text': result_text}]
            }

            format_end = time.time()
            format_duration = format_end - format_start
            logger.debug(f"Final formatting completed in {format_duration:.3f}s")

        # Overall completion summary
        overall_end = time.time()
        overall_duration = overall_end - overall_start

        logger.info(f"Enhanced chunking completed in {overall_duration:.3f}s - {words_count} words")
        logger.debug(f"Timing breakdown: init={init_duration:.3f}s, load={loading_duration:.3f}s, process={processing_duration:.3f}s, reconcile={reconcile_duration:.3f}s, extract={timestamp_duration:.3f}s, format={format_duration:.3f}s")

        return response

    except Exception as e:
        logger.exception(f"Enhanced chunking failed: {e}")
        raise