from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateProposalStorage(FileSystemStorage):
    def __init__(self, location=None, base_url=None):
        super().__init__(location=location, base_url=base_url)

    @property
    def location(self):
        configured_location = self._location or settings.PRIVATE_MEDIA_ROOT
        return str(Path(configured_location).resolve())

    def url(self, name):
        raise ValueError("Propostas privadas não possuem URL pública.")
