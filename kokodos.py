import copy
import json
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple
import signal

import numpy as np
import requests
import sounddevice as sd
from sounddevice import CallbackFlags
import yaml
from Levenshtein import distance
from loguru import logger

from kokodos import asr, tts, vad, video
from kokodos.vision import vision


logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

VAD_MODEL = "silero_vad.onnx"
PAUSE_TIME = 0.05  # Time to wait between processing loops
SAMPLE_RATE = 16000  # Sample rate for input stream
VAD_SIZE = 50  # Milliseconds of sample for Voice Activity Detection (VAD)
VAD_THRESHOLD = 0.9  # Threshold for VAD detection
BUFFER_SIZE = 600  # Milliseconds of buffer before VAD detection
PAUSE_LIMIT = 500  # Milliseconds of pause allowed before processing
SIMILARITY_THRESHOLD = 2  # Threshold for wake word similarity

DEFAULT_PERSONALITY_PREPROMPT = (
    {
        "role": "system",
        "content": "You are a helpful AI assistant. You are here to assist the user in their tasks.",
    },
)


@dataclass
class KokodosConfig:
    completion_url: str
    model: str
    api_key: Optional[str]
    wake_word: Optional[str]
    announcement: Optional[str]
    personality_preprompt: List[dict[str, str]]
    interruptible: bool
    tts_api_url: str
    tts_voice: str
    

    @classmethod
    def from_yaml(cls, path: str, key_to_config: Sequence[str] | None = ("Kokodos",)):
        key_to_config = key_to_config or []

        try:
            # First attempt with UTF-8
            with open(path, "r", encoding="utf-8") as file:
                data = yaml.safe_load(file)
        except UnicodeDecodeError:
            # Fall back to utf-8-sig if UTF-8 fails (handles BOM)
            with open(path, "r", encoding="utf-8-sig") as file:
                data = yaml.safe_load(file)

        config = data
        for nested_key in key_to_config:
            config = config[nested_key]

        return cls(**config)


class Kokodos:
    def __init__(
        self,
        completion_url: str,
        model: str,
        tts_voice: str,
        tts_api_url: str,
        api_key: str | None = None,
        wake_word: str | None = None,
        personality_preprompt: Sequence[dict[str, str]] = DEFAULT_PERSONALITY_PREPROMPT,
        announcement: str | None = None,
        interruptible: bool = True,
    ) -> None:
        """
        Initializes the VoiceRecognition class, setting up necessary models, streams, and queues.

        This class is not thread-safe, so you should only use it from one thread. It works like this:
        1. The audio stream is continuously listening for input.
        2. The audio is buffered until voice activity is detected. This is to make sure that the
            entire sentence is captured, including before voice activity is detected.
        2. While voice activity is detected, the audio is stored, together with the buffered audio.
        3. When voice activity is not detected after a short time (the PAUSE_LIMIT), the audio is
            transcribed. If voice is detected again during this time, the timer is reset and the
            recording continues.
        4. After the voice stops, the listening stops, and the audio is transcribed.
        5. If a wake word is set, the transcribed text is checked for similarity to the wake word.
        6. The function is called with the transcribed text as the argument.
        7. The audio stream is reset (buffers cleared), and listening continues.

        Args:
            wake_word (str, optional): The wake word to use for activation. Defaults to None.
        """

        if not completion_url:
            raise ValueError("completion_url is required")
        if not model:
            raise ValueError("model is required")
        
        self.shutdown_event = threading.Event()
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        self.completion_url = completion_url
        self.model = model
        self.wake_word = wake_word
        self._vad_model = vad.VAD(model_path=str(Path.cwd() / "models" / VAD_MODEL))
        self.raw_audio_queue = queue.Queue()
        self.vad_thread = threading.Thread(target=self._process_vad)
        self.vad_thread.start()
        self._asr_model = asr.AudioTranscriber()
        self._tts = tts.Synthesizer(voice=tts_voice, api_base=tts_api_url)

        # warm up onnx ASR model
        self._asr_model.transcribe_file("data/0.wav")

        # LLAMA_SERVER_HEADERS
        self.prompt_headers = {
            "Authorization": (
                f"Bearer {api_key}" if api_key else "Bearer your_api_key_here"
            ),
            "Content-Type": "application/json",
        }

        # Initialize sample queues and state flags
        self._samples: List[np.ndarray] = []
        self._sample_queue: queue.Queue[Tuple[np.ndarray, np.ndarray]] = queue.Queue()
        self._buffer: queue.Queue[np.ndarray] = queue.Queue(
            maxsize=BUFFER_SIZE // VAD_SIZE
        )
        self._recording_started = False
        self._gap_counter = 0

        self._messages = personality_preprompt
        self.llm_queue: queue.Queue[str] = queue.Queue()
        self.tts_queue: queue.Queue[str] = queue.Queue()
        self.processing = False
        self.currently_speaking = False
        self.interruptible = interruptible
        self._tts.rate= 24000
        
        self.latest_screenshot = None
        self.v_key_thread = threading.Thread(target=vision.monitor_v_key, daemon=True)
        self.v_key_thread.start()


        llm_thread = threading.Thread(target=self.process_LLM)
        llm_thread.start()

        tts_thread = threading.Thread(target=self.process_TTS_thread)
        tts_thread.start()
        self.video_processor = video.VideoProcessor()

        if announcement:
            audio = self._tts.generate_speech_audio(announcement)
            logger.success(f"TTS text: {announcement}")
            sd.play(audio, self._tts.rate)
            if not self.interruptible:
                sd.wait()

        self.input_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            callback=self.audio_callback_for_sdInputStream,
            blocksize=int(SAMPLE_RATE * VAD_SIZE / 1000),
        )
    def _signal_handler(self, signum, frame):
        """Handle signals to trigger graceful shutdown"""
        if self.shutdown_event.is_set():
            logger.warning("Forcing immediate shutdown!")
            sys.exit(1)
            
        logger.info(f"Received signal {signum}, initiating shutdown...")
        logger.remove()
        logger.add(sys.stderr, level="INFO")
        self.shutdown_event.set()
        sd.stop()
        self.input_stream.stop()
        if self.vad_thread.is_alive():
            self.vad_thread.join(timeout=1)
    def audio_callback_for_sdInputStream(self, indata: np.ndarray, frames: int, time: Any, status: CallbackFlags):
        try:
            data = indata.copy().squeeze()
            self.raw_audio_queue.put(data)
        except Exception as e:
            logger.error(f"Error in audio callback: {e}")
    
    def _process_vad(self):
        while not self.shutdown_event.is_set():
            try:
                data = self.raw_audio_queue.get(timeout=0.1)
                vad_value = self._vad_model.process_chunk(data)
                vad_confidence = vad_value > VAD_THRESHOLD
                self._sample_queue.put((data, vad_confidence))
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in VAD processing: {e}")

    @property
    def messages(self) -> Sequence[dict[str, str]]:
        return self._messages

    @classmethod
    def from_config(cls, config: KokodosConfig):
        personality_preprompt = []
        for line in config.personality_preprompt:
            personality_preprompt.append(
                {"role": list(line.keys())[0], "content": list(line.values())[0]}
            )

        return cls(
            completion_url=config.completion_url,
            model=config.model,
            tts_voice=config.tts_voice,
            tts_api_url=config.tts_api_url,
            api_key=config.api_key,
            wake_word=config.wake_word,
            personality_preprompt=personality_preprompt,
            announcement=config.announcement,
            interruptible=config.interruptible,
        )

    @classmethod
    def from_yaml(cls, path: str):
        return cls.from_config(KokodosConfig.from_yaml(path))

    def start_listen_event_loop(self):
        self.input_stream.start()
        logger.success("Audio Modules Operational")
        logger.success("Listening...")
        
        while not self.shutdown_event.is_set():
            try:
                sample, vad_confidence = self._sample_queue.get(timeout=0.1)
                self._handle_audio_sample(sample, vad_confidence)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                self.shutdown_event.set()

        self.input_stream.stop()

    def _handle_audio_sample(self, sample: np.ndarray, vad_confidence: bool):
        """
        Handles the processing of each audio sample.

        If the recording has not started, the sample is added to the circular buffer.

        If the recording has started, the sample is added to the samples list, and the pause
        limit is checked to determine when to process the detected audio.

        Args:
            sample (np.ndarray): The audio sample to process.
            vad_confidence (bool): Whether voice activity is detected in the sample.
        """
        if not self._recording_started:
            self._manage_pre_activation_buffer(sample, vad_confidence)
        else:
            self._process_activated_audio(sample, vad_confidence)

    def _manage_pre_activation_buffer(self, sample: np.ndarray, vad_confidence: bool):
        """
        Manages the circular buffer of audio samples before activation (i.e., before the voice is detected).

        If the buffer is full, the oldest sample is discarded to make room for new ones.

        If voice activity is detected, the audio stream is stopped, and the processing is turned off
        to prevent overlap with the LLM and TTS threads.

        Args:
            sample (np.ndarray): The audio sample to process.
            vad_confidence (bool): Whether voice activity is detected in the sample.
        """
        if self._buffer.full():
            self._buffer.get()  # Discard the oldest sample to make room for new ones
        self._buffer.put(sample)

        if vad_confidence:  # Voice activity detected
            sd.stop()  # Stop the audio stream to prevent overlap
            self.processing = (
                False  # Turns off processing on threads for the LLM and TTS!!!
            )
            self._samples = list(self._buffer.queue)
            self._recording_started = True
            self.video_processor.start_recording()

    def _process_activated_audio(self, sample: np.ndarray, vad_confidence: bool):
        """
        Processes audio samples after activation (i.e., after the wake word is detected).

        Uses a pause limit to determine when to process the detected audio. This is to
        ensure that the entire sentence is captured before processing, including slight gaps.
        """

        self._samples.append(sample)

        if not vad_confidence:
            self._gap_counter += 1
            if self._gap_counter >= PAUSE_LIMIT // VAD_SIZE:
                self._process_detected_audio()
        else:
            self._gap_counter = 0

    def _wakeword_detected(self, text: str) -> bool:
        """
        Calculates the nearest Levenshtein distance from the detected text to the wake word.

        This is used as 'Kokodos' is not a common word, and Whisper can sometimes mishear it.
        """
        assert self.wake_word is not None, "Wake word should not be None"

        words = text.split()
        closest_distance = min(
            [distance(word.lower(), self.wake_word) for word in words]
        )
        return closest_distance < SIMILARITY_THRESHOLD

    def _process_detected_audio(self):
        """
        Processes the detected audio and generates a response.

        This function is called when the pause limit is reached after the voice stops.
        It transcribes the audio and checks for the wake word if it is set. If the wake
        word is detected, the detected text is sent to the LLM model for processing.
        The audio stream is then reset, and listening continues.
        """
        if self.shutdown_event.is_set():
            return
        
        logger.debug("Detected pause after speech. Processing...")
        self.input_stream.stop()
        self.video_processor.stop_recording()
        detected_text = self.asr(self._samples)

        if detected_text:
            logger.success(f"ASR text: '{detected_text}'")

            if self.wake_word and not self._wakeword_detected(detected_text):
                logger.info(f"Required wake word {self.wake_word=} not detected.")
            else:
                self.llm_queue.put(detected_text)
                self.processing = True
                self.currently_speaking = True

        if not self.interruptible:
            while self.currently_speaking:
                time.sleep(PAUSE_TIME)

        self.reset()
        self.input_stream.start()

    def asr(self, samples: List[np.ndarray]) -> str:
        """
        Performs automatic speech recognition on the collected samples.
        """
        audio = np.concatenate(samples)

        detected_text = self._asr_model.transcribe(audio)
        return detected_text

    def reset(self):
        """
        Resets the recording state and clears buffers.
        """
        logger.debug("Resetting recorder...")
        self._recording_started = False
        self._samples.clear()
        self._gap_counter = 0
        with self._buffer.mutex:
            self._buffer.queue.clear()

    def process_TTS_thread(self):
        """
        Processes the LLM generated text using the TTS model.

        Runs in a separate thread to allow for continuous processing of the LLM output.
        """
        assistant_text = []  # The text generated by the assistant, to be spoken by the TTS
        system_text = []  # The text logged to the system prompt when the TTS is interrupted
        finished = False  # a flag to indicate when the TTS has finished speaking
        interrupted = (
            False  # a flag to indicate when the TTS was interrupted by new input
        )

        while not self.shutdown_event.is_set():
            try:
                start = time.time()
                generated_text = self.tts_queue.get(timeout=PAUSE_TIME)

                if (
                    generated_text == "<EOS>"
                ):  # End of stream token generated in process_LLM_thread
                    finished = True
                elif not generated_text:
                    logger.warning("Empty string sent to TTS")  # should not happen!
                else:
                    logger.success(f"LLM text: {generated_text}")
                    logger.info(f"LLM inference time: {(time.time() - start):.2f}s")
                    start = time.time()
                    audio = self._tts.generate_speech_audio(generated_text)
                    logger.info(
                        f"TTS Complete, inference: {(time.time() - start):.2f}, length: {len(audio) / self._tts.rate:.2f}s"
                    )
                    total_samples = len(audio)

                    if total_samples:
                        sd.play(audio, self._tts.rate)

                        interrupted, percentage_played = self.percentage_played(
                            total_samples
                        )

                        if interrupted:
                            clipped_text = self.clip_interrupted_sentence(
                                generated_text, percentage_played
                            )

                            logger.info(
                                f"TTS interrupted at {percentage_played}%: {clipped_text}"
                            )
                            system_text = copy.deepcopy(assistant_text)
                            system_text.append(clipped_text)
                            finished = True

                        assistant_text.append(generated_text)

                if finished:
                    self.messages.append(
                        {"role": "assistant", "content": " ".join(assistant_text)}
                    )
                    # if interrupted:
                    #     self.messages.append(
                    #         {
                    #             "role": "system",
                    #             "content": f"USER INTERRUPTED KOKODOS, TEXT DELIVERED: {' '.join(system_text)}",
                    #         }
                    #     )
                    assistant_text = []
                    finished = False
                    interrupted = False
                    self.currently_speaking = False

            except queue.Empty:
                pass

    def clip_interrupted_sentence(
        self, generated_text: str, percentage_played: float
    ) -> str:
        """
        Clips the generated text if the TTS was interrupted.

        Args:

            generated_text (str): The generated text from the LLM model.
            percentage_played (float): The percentage of the audio played before the TTS was interrupted.

            Returns:

            str: The clipped text.

        """
        tokens = generated_text.split()
        words_to_print = round((percentage_played / 100) * len(tokens))
        text = " ".join(tokens[:words_to_print])

        # If the TTS was cut off, make that clear
        if words_to_print < len(tokens):
            text = text + "<INTERRUPTED>"
        return text

    def percentage_played(self, total_samples: int) -> Tuple[bool, int]:
        interrupted = False
        start_time = time.time()
        played_samples = 0.0

        try:
            stream = sd.get_stream()

            while stream and stream.active:
                time.sleep(PAUSE_TIME)
                if self.processing is False:
                    sd.stop()
                    with self.tts_queue.mutex:
                        self.tts_queue.queue.clear()
                    interrupted = True
                    break

                try:
                    if not stream.active:
                        break
                except sd.PortAudioError:
                    break

        except (sd.PortAudioError, RuntimeError):
            logger.debug("Audio stream already closed or invalid")

        elapsed_time = (
            time.time() - start_time + 0.12
        )  # slight delay to ensure all audio timing is correct
        played_samples = elapsed_time * self._tts.rate

        # Calculate percentage of audio played
        percentage_played = min(int((played_samples / total_samples * 100)), 100)
        return interrupted, percentage_played

    def process_LLM(self):
        while not self.shutdown_event.is_set():
            try:
                detected_text = self.llm_queue.get(timeout=0.1)
                video_frames = self.video_processor.get_frames()
                
                with vision.screenshot_lock:
                    vision_image = [vision.latest_screenshot] if vision.latest_screenshot else []
                    vision.latest_screenshot = None

                all_images = vision_image + video_frames
                message_content = {
                    "role": "user",
                    "content": detected_text
                }
                
                if all_images:
                    message_content["images"] = all_images
                    logger.success(f"Sending {len(all_images)} images with query")

                self.messages.append(message_content)
                all_images = None
                logger.debug(f"Sending to LLM: {json.dumps(message_content, indent=2)[:500]}...")
            
                data = {
                    "model": self.model,
                    "stream": True,
                    "messages": self.messages,
                }
                logger.debug(f"starting request on {self.messages=}")
                logger.debug("Performing request to LLM server...")
                self.latest_screenshot = None
                try:
                    with requests.post(
                        self.completion_url,
                        headers=self.prompt_headers,
                        json=data,
                        stream=True,
                        timeout=10
                    ) as response:
                        response.raise_for_status()
                        sentence = []
                        for line in response.iter_lines():
                            if self.processing is False:
                                break  # If the stop flag is set from new voice input, halt processing
                            if line:  # Filter out empty keep-alive new lines
                                try:
                                    cleaned_line = self._clean_raw_bytes(line)
                                    if cleaned_line:  # Add check for empty cleaned line
                                        chunk = self._process_chunk(cleaned_line)
                                        if chunk:
                                            sentence.append(chunk)
                                            # If there is a pause token, send the sentence to the TTS queue
                                            if chunk in [
                                                #",",
                                                ".",
                                                "!",
                                                "?",
                                                ":",
                                                ";",
                                                "?!",
                                                "\n",
                                                "\n\n",
                                            ]:
                                                self._process_sentence(sentence)
                                                sentence = []
                                except Exception as e:
                                    logger.error(f"Error processing line: {e}")
                                    continue
                except requests.exceptions.RequestException as e:
                    logger.error(f"LLM API request failed: {e}")
                    self.tts_queue.put("I'm having trouble connecting to the Ollama API.")
                    continue
                if self.processing and sentence:
                    self._process_sentence(sentence)
                    self.tts_queue.put("<EOS>")  # Add end of stream token to the queue
            except queue.Empty:
                time.sleep(PAUSE_TIME)

    def _process_sentence(self, current_sentence: List[str]):
        """
        Join text, remove inflections and actions, and send to the TTS queue.

        The LLM like to *whisper* things or (scream) things, and prompting is not a 100% fix.
        We use regular expressions to remove text between ** and () to clean up the text.
        Finally, we remove any non-alphanumeric characters/punctuation and send the text
        to the TTS queue.
        """
        sentence = "".join(current_sentence)
        sentence = re.sub(r"\*.*?\*|\(.*?\)", "", sentence)
        sentence = (
            sentence.replace("\n\n", ". ")
            .replace("\n", ". ")
            .replace("  ", " ")
            .replace(":", " ")
        )
        if sentence:
            self.tts_queue.put(sentence)

    def _clean_raw_bytes(self, line):
        """
        Cleans the raw bytes from the server and converts to OpenAI format.

        Args:
            line (bytes): The raw bytes from the server

        Returns:
            dict or None: Parsed JSON response in OpenAI format, or None if parsing fails
        """
        try:
            # Handle OpenAI format
            if line.startswith(b"data: "):
                json_str = line.decode("utf-8")[6:]  # Remove 'data: ' prefix
                return json.loads(json_str)
            # Handle Ollama format
            else:
                return json.loads(line.decode("utf-8"))
        except Exception as e:
            logger.warning(f"Failed to parse server response: {e}")
            return None

    def _process_chunk(self, line):
        """
        Processes a single line of text from the LLM server.

        Args:
            line (dict): The line of text from the LLM server.
        """
        if not line or not isinstance(line, dict):
            return None

        try:
            # Handle OpenAI format
            if "choices" in line:
                content = line.get("choices", [{}])[0].get("delta", {}).get("content")
                return content if content else None
            # Handle Ollama format
            else:
                content = line.get("message", {}).get("content")
                return content if content else None
        except Exception as e:
            logger.error(f"Error processing chunk: {e}")
            return None


def start() -> None:
    try:
        kokodos_config = KokodosConfig.from_yaml("kokodos_config.yml")
        kokodos = Kokodos.from_config(kokodos_config)
        kokodos.start_listen_event_loop()
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
    finally:
        logger.info("Shutting down gracefully...")

if __name__ == "__main__":
    start()