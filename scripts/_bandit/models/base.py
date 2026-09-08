# Adapted for inference: replace the empty Lightning base with torch.nn.Module.
from torch import nn


class BaseEndToEndModule(nn.Module):
    def __init__(
        self,
    ) -> None:
        super().__init__()
