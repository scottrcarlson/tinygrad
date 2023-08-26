# thanks to https://github.com/openai/whisper for a good chunk of MIT licensed code

#pip install SpeechRecognition

import sys
import pathlib
import base64
import numpy as np
from typing import Optional
from extra.utils import download_file
from tinygrad.nn.state import torch_load, load_state_dict
import tinygrad.nn as nn
from tinygrad.tensor import Tensor
import itertools
import librosa
import warnings
import time
import argparse
from queue import Queue
from tempfile import NamedTemporaryFile
import speech_recognition as sr
import io
import os
import cProfile

warnings.filterwarnings('ignore')

# TODO: you have written this fifteen times
class MultiHeadAttention:
  def __init__(self, n_state, n_head):
    self.n_head = n_head
    self.query = nn.Linear(n_state, n_state)
    self.key = nn.Linear(n_state, n_state, bias=False)
    self.value = nn.Linear(n_state, n_state)
    self.out = nn.Linear(n_state, n_state)

  def __call__(self, x:Tensor, xa:Optional[Tensor]=None, mask:Optional[Tensor]=None):
    q = self.query(x)
    k = self.key(xa or x)
    v = self.value(xa or x)
    wv, qk = self.qkv_attention(q, k, v, mask)
    # NOTE: we aren't returning qk
    return self.out(wv)

  def qkv_attention(self, q, k, v, mask=None):
    n_batch, n_ctx, n_state = q.shape
    scale = (n_state // self.n_head) ** -0.25
    q = q.reshape(*q.shape[:2], self.n_head, -1).permute(0, 2, 1, 3) * scale
    k = k.reshape(*k.shape[:2], self.n_head, -1).permute(0, 2, 3, 1) * scale
    v = v.reshape(*v.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)
    qk = q @ k
    if mask is not None: qk = qk + mask[:n_ctx, :n_ctx]
    w = qk.softmax(-1)
    return (w @ v).permute(0, 2, 1, 3).flatten(start_dim=2), qk.detach()

class ResidualAttentionBlock:
  def __init__(self, n_state, n_head, cross_attention=False):
    self.attn = MultiHeadAttention(n_state, n_head)
    self.attn_ln = nn.LayerNorm(n_state)

    self.cross_attn = MultiHeadAttention(n_state, n_head) if cross_attention else None
    self.cross_attn_ln = nn.LayerNorm(n_state) if cross_attention else None

    self.mlp = [nn.Linear(n_state, n_state*4), Tensor.gelu, nn.Linear(n_state*4, n_state)]
    self.mlp_ln = nn.LayerNorm(n_state)

  def __call__(self, x, xa=None, mask=None):
    x = x + self.attn(self.attn_ln(x), mask=mask)
    if self.cross_attn: x = x + self.cross_attn(self.cross_attn_ln(x), xa)
    x = x + self.mlp_ln(x).sequential(self.mlp)
    return x

class AudioEncoder:
  def __init__(self, n_mels, n_audio_ctx, n_audio_state, n_audio_head, n_audio_layer, **_):
    self.conv1 = nn.Conv1d(n_mels, n_audio_state, kernel_size=3, padding=1)
    self.conv2 = nn.Conv1d(n_audio_state, n_audio_state, kernel_size=3, stride=2, padding=1)
    self.blocks = [ResidualAttentionBlock(n_audio_state, n_audio_head) for _ in range(n_audio_layer)]
    self.ln_post = nn.LayerNorm(n_audio_state)
    self.positional_embedding = Tensor.empty(n_audio_ctx, n_audio_state)

  def __call__(self, x):
    x = self.conv1(x).gelu()
    x = self.conv2(x).gelu()
    x = x.permute(0, 2, 1)
    x = x + self.positional_embedding[:x.shape[1]]
    x = x.sequential(self.blocks)
    x = self.ln_post(x)
    return x

class TextDecoder:
  def __init__(self, n_vocab, n_text_ctx, n_text_state, n_text_head, n_text_layer, **_):
    self.token_embedding = nn.Embedding(n_vocab, n_text_state)
    self.positional_embedding = Tensor.empty(n_text_ctx, n_text_state)
    self.blocks = [ResidualAttentionBlock(n_text_state, n_text_head, cross_attention=True) for _ in range(n_text_layer)]
    self.ln = nn.LayerNorm(n_text_state)
    self.temperature = 2
    #mask = torch.empty(n_ctx, n_ctx).fill_(-np.inf).triu_(1)

  def __call__(self, x, xa):
    offset = 0
    x = self.token_embedding(x) + self.positional_embedding[offset : offset + x.shape[-1]]

    seqlen, start_pos = x.shape[1], 0

    mask = np.full((1, 1, seqlen, start_pos + seqlen), float("-inf"), dtype=np.float32)
    mask = np.triu(mask, k=start_pos + 1)  # TODO: this is hard to do in tinygrad
    mask = Tensor(mask)

    for block in self.blocks: x = block(x, xa, mask)
    x = self.ln(x)

    #logits = x @ self.token_embedding.weight.T
    #return logits / self.temperature
    return x @ self.token_embedding.weight.T

class Whisper:
  def __init__(self, dims):
    self.encoder = AudioEncoder(**dims)
    self.decoder = TextDecoder(**dims)

  def __call__(self, mel:Tensor, tokens:Tensor):
    return self.decoder(tokens, self.encoder(mel))

def prep_audio(waveform=None, sr=16000) -> Tensor:
  N_FFT = 400
  HOP_LENGTH = 160
  N_MELS = 80
  if waveform is None: waveform = np.zeros(N_FFT, dtype=np.float32)
  stft = librosa.stft(waveform, n_fft=N_FFT, hop_length=HOP_LENGTH, window='hann', dtype=np.float32)
  magnitudes = stft[..., :-1] ** 2
  mel_spec = librosa.filters.mel(sr=sr, n_fft=N_FFT, n_mels=N_MELS) @ magnitudes
  log_spec = np.log10(np.clip(mel_spec, 1e-10, mel_spec.max() + 1e8))
  log_spec = (log_spec + 4.0) / 4.0
  #print(waveform.shape, log_spec.shape)
  log_spec = log_spec[np.newaxis, :, :]
  return log_spec

LANGUAGES = {
  "en": "english", "zh": "chinese", "de": "german", "es": "spanish", "ru": "russian", "ko": "korean", "fr": "french", "ja": "japanese", "pt": "portuguese", "tr": "turkish",
  "pl": "polish", "ca": "catalan", "nl": "dutch", "ar": "arabic", "sv": "swedish", "it": "italian", "id": "indonesian", "hi": "hindi", "fi": "finnish", "vi": "vietnamese",
  "he": "hebrew", "uk": "ukrainian", "el": "greek", "ms": "malay", "cs": "czech", "ro": "romanian", "da": "danish", "hu": "hungarian", "ta": "tamil", "no": "norwegian",
  "th": "thai", "ur": "urdu", "hr": "croatian", "bg": "bulgarian", "lt": "lithuanian", "la": "latin", "mi": "maori", "ml": "malayalam", "cy": "welsh", "sk": "slovak", "te": "telugu",
  "fa": "persian", "lv": "latvian", "bn": "bengali", "sr": "serbian", "az": "azerbaijani", "sl": "slovenian", "kn": "kannada", "et": "estonian", "mk": "macedonian",
  "br": "breton", "eu": "basque", "is": "icelandic", "hy": "armenian", "ne": "nepali", "mn": "mongolian", "bs": "bosnian", "kk": "kazakh", "sq": "albanian", "sw": "swahili",
  "gl": "galician", "mr": "marathi", "pa": "punjabi", "si": "sinhala", "km": "khmer", "sn": "shona", "yo": "yoruba", "so": "somali", "af": "afrikaans", "oc": "occitan", "ka": "georgian",
  "be": "belarusian", "tg": "tajik", "sd": "sindhi", "gu": "gujarati", "am": "amharic", "yi": "yiddish", "lo": "lao", "uz": "uzbek", "fo": "faroese", "ht": "haitian creole",
  "ps": "pashto", "tk": "turkmen", "nn": "nynorsk", "mt": "maltese", "sa": "sanskrit", "lb": "luxembourgish", "my": "myanmar", "bo": "tibetan", "tl": "tagalog", "mg": "malagasy",
  "as": "assamese", "tt": "tatar", "haw": "hawaiian", "ln": "lingala", "ha": "hausa", "ba": "bashkir", "jw": "javanese", "su": "sundanese",
}

MODELS = {
  "tiny.en": "https://openaipublic.azureedge.net/main/whisper/models/d3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03/tiny.en.pt",
  "tiny": "https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9/tiny.pt",
  "base.en": "https://openaipublic.azureedge.net/main/whisper/models/25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead/base.en.pt",
  "base": "https://openaipublic.azureedge.net/main/whisper/models/ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e/base.pt",
  "small.en": "https://openaipublic.azureedge.net/main/whisper/models/f953ad0fd29cacd07d5a9eda5624af0f6bcf2258be67c92b79389873d91e0872/small.en.pt",
  "small": "https://openaipublic.azureedge.net/main/whisper/models/9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794/small.pt",
  "medium.en": "https://openaipublic.azureedge.net/main/whisper/models/d7440d1dc186f76616474e0ff0b3b6b879abc9d1a4926b7adfa41db2d497ab4f/medium.en.pt",
  "medium": "https://openaipublic.azureedge.net/main/whisper/models/345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1/medium.pt",
  "large-v1": "https://openaipublic.azureedge.net/main/whisper/models/e4b87e7e0bf463eb8e6956e646f1e277e901512310def2c24bf0e11bd3c28e9a/large-v1.pt",
  "large-v2": "https://openaipublic.azureedge.net/main/whisper/models/81f7c96c852ee8fc832187b0132e569d6c3065a3252ed18e56effd0b6a73e524/large-v2.pt",
  "large": "https://openaipublic.azureedge.net/main/whisper/models/81f7c96c852ee8fc832187b0132e569d6c3065a3252ed18e56effd0b6a73e524/large-v2.pt",
}

_ALIGNMENT_HEADS = {
    "tiny.en": b"ABzY8J1N>@0{>%R00Bk>$p{7v037`oCl~+#00",
    "tiny": b"ABzY8bu8Lr0{>%RKn9Fp%m@SkK7Kt=7ytkO",
    "base.en": b"ABzY8;40c<0{>%RzzG;p*o+Vo09|#PsxSZm00",
    "base": b"ABzY8KQ!870{>%RzyTQH3`Q^yNP!>##QT-<FaQ7m",
    "small.en": b"ABzY8>?_)10{>%RpeA61k&I|OI3I$65C{;;pbCHh0B{qLQ;+}v00",
    "small": b"ABzY8DmU6=0{>%Rpa?J`kvJ6qF(V^F86#Xh7JUGMK}P<N0000",
    "medium.en": b"ABzY8usPae0{>%R7<zz_OvQ{)4kMa0BMw6u5rT}kRKX;$NfYBv00*Hl@qhsU00",
    "medium": b"ABzY8B0Jh+0{>%R7}kK1fFL7w6%<-Pf*t^=N)Qr&0RR9",
    "large-v1": b"ABzY8r9j$a0{>%R7#4sLmoOs{s)o3~84-RPdcFk!JR<kSfC2yj",
    "large-v2": b"ABzY8zd+h!0{>%R7=D0pU<_bnWW*tkYAhobTNnu$jnkEkXqp)j;w1Tzk)UH3X%SZd&fFZ2fC2yj",
    "large": b"ABzY8zd+h!0{>%R7=D0pU<_bnWW*tkYAhobTNnu$jnkEkXqp)j;w1Tzk)UH3X%SZd&fFZ2fC2yj",
}

BASE = pathlib.Path(__file__).parent.parent / "weights"
def get_encoding(n_vocab_in):
  download_file("https://raw.githubusercontent.com/openai/whisper/main/whisper/assets/gpt2.tiktoken", BASE / "gpt2.tiktoken")
  ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in open(BASE / "gpt2.tiktoken") if line)}
  n_vocab = len(ranks)
  specials = [
    "<|endoftext|>",
    "<|startoftranscript|>",
    *[f"<|{lang}|>" for lang in LANGUAGES.keys()],
    "<|translate|>",
    "<|transcribe|>",
    "<|startoflm|>",
    "<|startofprev|>",
    "<|nospeech|>",
    "<|notimestamps|>",
    *[f"<|{i * 0.02:.2f}|>" for i in range(1501)],
  ]

 
  special_tokens = dict(zip(specials, itertools.count(n_vocab)))
  n_vocab += len(specials)
  assert n_vocab == n_vocab_in
  import tiktoken
  return tiktoken.Encoding(
    name="bob",
    explicit_n_vocab=n_vocab,
    pat_str=r"""'s|'t|'re|'ve|'m|'ll|'d| ?\p{L}+| ?\p{N}+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+""",
    mergeable_ranks=ranks,
    special_tokens=special_tokens)

def img(x):
  import matplotlib.pyplot as plt
  plt.imshow(x.numpy())
  plt.show()


def print_statusline(msg: str):
    last_msg_length = len(getattr(print_statusline, 'last_msg', ''))
    print(' ' * last_msg_length, end='\r')
    print(msg, end='\r')
    sys.stdout.flush()  # Some say they needed this, I didn't.
    setattr(print_statusline, 'last_msg', msg)


def is_silent(data, threshold=0.02):
  """Returns 'True' if below the 'silent' threshold"""
  return np.mean(np.abs(data)) < threshold

def clear_line(n=1):
  LINE_UP = '\033[1A'
  LINE_CLEAR = '\x1b[2K'
  for i in range(n):
    print(LINE_UP, end=LINE_CLEAR)

def remove_tokens(text, tokens_to_remove):
    for token in tokens_to_remove:
        text = text.replace(token, "")
    return text.strip() 


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("--model", default="medium", help="Model to use",
                      choices=["tiny", "base", "small", "medium", "large"])
  parser.add_argument("--record_time", default=2, help="recording time in seconds",type=float)
  parser.add_argument("--phrase_timeout", default=3, help="how much empty space before deciding a new line in transcript",type=float)
  parser.add_argument("--energy_threshold", default=1000,
                        help="Energy level for mic to detect.", type=int)

  if 'linux' in sys.platform:
    parser.add_argument("--default_microphone", default='pulse',
                help="Default microphone name for SpeechRecognition. "
                  "Run this with 'list' to view available Microphones.", type=str)
  args = parser.parse_args()

  #The last time a srecording was retrieved from the queue
  phrase_time = None
  # Current raw audio bytes
  #last_sample = bytes()
  last_sample = None
  # Thread safe Queue for passing data from threaded recording callback
  data_queue = Queue()

  #We use SpeechRecorder to record and detect end of speech
  recorder = sr.Recognizer()
  recorder.energy_threshold = 1000
  recorder.dynamic_energy_threshold = False   # dynamic energy compensation end of speech threshold

  if 'linux' in sys.platform:
    mic_name = args.default_microphone
    if not mic_name or mic_name == 'list':
      print("Available mic devices:")
      for index, name in enumerate(sr.Microphone.list_microphone_names()):
        print(f"Microphone with name \"{name}\" found")
    else:
      for index, name in enumerate(sr.Microphone.list_microphone_names()):
        if mic_name in name:
          source = sr.Microphone(sample_rate=16000, device_index=index)
          break
  else:
    source = sr.Microphone(sample_rate=16000)

  #force english
  model_name = args.model + ".en"


  fn = BASE / "whisper-" / model_name / ".en.pt"
  download_file(MODELS[model_name], fn)
  state = torch_load(fn)
  model = Whisper(state['dims'])
  load_state_dict(model, state['model_state_dict'])
  enc = get_encoding(state['dims']['n_vocab'])
  record_time = args.record_time
  phrase_timeout = args.phrase_timeout

  temp_file = NamedTemporaryFile().name
  transcription = ['']

  with source:
    recorder.adjust_for_ambient_noise(source)
  
  def record_callback(_, audio:sr.AudioData) -> None:
    print("record callback")
    data = audio.get_raw_data()
    data_queue.put_nowait(data)

  recorder.listen_in_background(source, record_callback, phrase_time_limit=record_time)
  print("whisper model loaded")

  filtered_tokens = ["<|startoftranscript|>","<|endoftext|>","<|0.00|>","<|nospeech|>","<|notimestamps|>"]
  while True:
    try:
      now = time.time()
      if not data_queue.empty():
        phrase_complete = False
        phrase_time = now

        while not data_queue.empty():
          waveform = data_queue.get()
          if last_sample == None:
            last_sample = waveform
          else:
            last_sample += waveform

        audio_data = sr.AudioData(last_sample, source.SAMPLE_RATE, source.SAMPLE_WIDTH)
        wav_data = io.BytesIO(audio_data.get_wav_data())

        #with open("test.wav", 'w+b') as f:
        #  f.write(wav_data.read())

        waveform, sample_rate = librosa.load(wav_data)
        log_spec = prep_audio(waveform, sample_rate)
        encoded_audio = model.encoder(Tensor(log_spec)).realize()
        
        #profiler = cProfile.Profile()
        #profiler.enable()
        lst = [enc._special_tokens["<|startoftranscript|>"]]
        while not phrase_complete:
          try:
            decoded_text = model.decoder(Tensor([lst]), encoded_audio) #.realize()
            idx = int(decoded_text[0,-1].argmax().numpy())
            lst.append(idx)
            text = enc.decode(lst)

            #text_notoken = remove_tokens(text, filtered_tokens)
            print(text)
            
     
            #print(f"Checking text: '{text}'")
            #for token in filtered_tokens:
            #    print(f"Checking token: '{token}' in text: {token in text}")
            if time.time() - phrase_time > phrase_timeout or "<|endoftext|>" in text:
              phrase_complete = True
              last_sample = None
      
          except KeyboardInterrupt:
            break
        #profiler.disable()
        #profiler.dump_stats("output.pstats")

        if phrase_complete:
          transcription.append(text)
        else:
          transcription[-1] = text

        

        #os.system('cls' if os.name=='nt' else 'clear')
        #print("-----------------------------")
        #for line in transcription:
        #  print(line)
       # 
       # print('', end='', flush=True)
    except KeyboardInterrupt:
      break

  print("\n\nTranscription:")
  for line in transcription:
    print(line)

if __name__ == "__main__":
  main()