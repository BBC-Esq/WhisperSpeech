# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/6. Quality-boosting vocoder.ipynb.

# %% auto 0
__all__ = ['Vocoder']

# %% ../nbs/6. Quality-boosting vocoder.ipynb 1
from vocos import Vocos
from whisperspeech import inference
import torch
import torchaudio

# %% ../nbs/6. Quality-boosting vocoder.ipynb 2
class Vocoder:
    def __init__(self, repo_id="charactr/vocos-encodec-24khz", device=None, cache_dir=None):
        if device is None: device = inference.get_compute_device()
        if device == 'mps': device = 'cpu' # mps does not currently work with vocos, thus only cuda or cpu
        self.device = device
        self.vocos = Vocos.from_pretrained(repo_id).to(device)

    def is_notebook(self):
        try:
            return get_ipython().__class__.__name__ == "ZMQInteractiveShell"
        except:
            return False

    @torch.no_grad()
    def decode(self, atoks):
        if len(atoks.shape) == 3:
            b,q,t = atoks.shape
            
            atoks = atoks.permute(1,0,2)
        else:
            q,t = atoks.shape
        # on mps we run Vocos on the CPU, make sure it's input is on the correct device
        atoks = atoks.to(self.device)
        # print(atoks.dtype, atoks.device) # uncomment to check dtype and compute_device
        features = self.vocos.codes_to_features(atoks)
        bandwidth_id = torch.tensor({2: 0, 4: 1, 8: 2}[q]).to(self.device)  # Move tensor to the same device as model
        return self.vocos.decode(features, bandwidth_id=bandwidth_id)
        
    def decode_to_file(self, fname, atoks):
        audio = self.decode(atoks)
        torchaudio.save(fname, audio.cpu(), 24000)
        if self.is_notebook():
            from IPython.display import display, HTML, Audio
            display(HTML(f'<a href="{fname}" target="_blank">Listen to {fname}</a>'))
        
    def decode_to_notebook(self, atoks):
        from IPython.display import display, HTML, Audio

        audio = self.decode(atoks)
        display(Audio(audio.cpu().numpy(), rate=24000))
