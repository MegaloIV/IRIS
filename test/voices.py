from kokoro_onnx import Kokoro
k = Kokoro("data/kokoro/kokoro-v0_19.fp16.onnx", "data/kokoro/voices.bin")
print(k.get_voices())