import warnings

from PIL import Image, UnidentifiedImageError
from django.core.exceptions import ValidationError

MAX_PROPOSAL_IMAGE_SIZE = 5 * 1024 * 1024
ALLOWED_PROPOSAL_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP"}


def get_proposal_image_format(value):
    if value.size > MAX_PROPOSAL_IMAGE_SIZE:
        raise ValidationError("A imagem deve ter no máximo 5 MB.")

    image_file = getattr(value, "file", value)
    try:
        original_position = image_file.tell()
        image_file.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(image_file) as image:
                image_format = image.format
                image.verify()

            image_file.seek(0)
            with Image.open(image_file) as image:
                image.load()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ) as exc:
        raise ValidationError("O arquivo enviado não é uma imagem válida.") from exc
    finally:
        if "original_position" in locals():
            image_file.seek(original_position)

    if image_format not in ALLOWED_PROPOSAL_IMAGE_FORMATS:
        raise ValidationError(
            "Envie uma imagem PNG, JPEG ou WebP."
        )

    return image_format


def validate_proposal_image(value):
    get_proposal_image_format(value)
