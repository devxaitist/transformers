# coding=utf-8
# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Fast Image processor class for Vilt."""

from typing import List, Optional, Union

from ...image_processing_utils import BatchFeature
from ...image_processing_utils_fast import (
    BASE_IMAGE_PROCESSOR_FAST_DOCSTRING,
    BaseImageProcessorFast,
    DefaultFastImageProcessorKwargs,
    get_max_height_width,
    group_images_by_shape,
    reorder_images,
)
from ...image_utils import IMAGENET_STANDARD_MEAN, IMAGENET_STANDARD_STD, PILImageResampling, SizeDict
from ...utils import (
    TensorType,
    add_start_docstrings,
    is_torch_available,
    is_torchvision_available,
    is_torchvision_v2_available,
)


if is_torch_available():
    import torch

if is_torchvision_available():
    if is_torchvision_v2_available():
        from torchvision.transforms.v2 import functional as F
    else:
        from torchvision.transforms import functional as F

# Set maximum size based on the typical aspect ratio of the COCO dataset
MAX_LONGER_EDGE = 1333
MAX_SHORTER_EDGE = 800


class ViltFastImageProcessorKwargs(DefaultFastImageProcessorKwargs):
    do_pad: Optional[bool]
    size_divisor: Optional[int]
    rescale_factor: Optional[float]


@add_start_docstrings(
    "Constructs a fast Vilt image processor.",
    BASE_IMAGE_PROCESSOR_FAST_DOCSTRING,
)
class ViltImageProcessorFast(BaseImageProcessorFast):
    # This generated class can be used as a starting point for the fast image processor.
    # if the image processor is only used for simple augmentations, such as resizing, center cropping, rescaling, or normalizing,
    # only the default values should be set in the class.
    # If the image processor requires more complex augmentations, methods from BaseImageProcessorFast can be overridden.
    # In most cases, only the `_preprocess` method should be overridden.

    # For an example of a fast image processor requiring more complex augmentations, see `LlavaNextImageProcessorFast`.

    # Default values should be checked against the slow image processor
    # None values left after checking can be removed
    resample = PILImageResampling.BICUBIC
    image_mean = IMAGENET_STANDARD_MEAN
    image_std = IMAGENET_STANDARD_STD
    size = {"shortest_edge": 384}
    do_resize = True
    do_rescale = True
    do_normalize = True
    size_divisor = 32
    do_pad = True
    default_to_square = False
    model_input_names = ["pixel_values", "pixel_mask"]
    valid_kwargs = ViltFastImageProcessorKwargs

    def _preprocess(
        self,
        images: list["torch.Tensor"],
        do_resize: bool,
        size: SizeDict,
        interpolation: Optional["F.InterpolationMode"],
        crop_size: SizeDict,
        do_rescale: bool,
        rescale_factor: float,
        do_normalize: bool,
        image_mean: Optional[Union[float, List[float]]],
        image_std: Optional[Union[float, List[float]]],
        return_tensors: Optional[Union[str, TensorType]],
        **kwargs,
    ) -> BatchFeature:
        """
        Preprocess an image or batch of images.

        This method overrides the base class method to include padding and pixel mask generation.
        """
        size_divisor = kwargs.get("size_divisor", self.size_divisor)
        do_pad = kwargs.get("do_pad", self.do_pad)

        # Group images by size for batched resizing
        grouped_images, grouped_images_index = group_images_by_shape(images)
        resized_images_grouped = {}

        for shape, stacked_images in grouped_images.items():
            if do_resize:
                # Resize with aspect ratio preservation
                shorter = size.shortest_edge
                longer = int(MAX_LONGER_EDGE / MAX_SHORTER_EDGE * shorter)

                heights = stacked_images.shape[-2]
                widths = stacked_images.shape[-1]

                # Determine the new dimensions
                if heights < widths:
                    new_heights = shorter
                    new_widths = widths * (shorter / heights)
                else:
                    new_heights = heights * (shorter / widths)
                    new_widths = shorter

                # Check if the longer side exceeds max size
                if max(new_heights, new_widths) > longer:
                    scale = longer / max(new_heights, new_widths)
                    new_heights = new_heights * scale
                    new_widths = new_widths * scale

                new_heights = int(new_heights + 0.5)
                new_widths = int(new_widths + 0.5)
                # Make dimensions divisible by size_divisor
                if size_divisor is not None:
                    new_heights = new_heights // size_divisor * size_divisor
                    new_widths = new_widths // size_divisor * size_divisor

                # Resize the image
                stacked_images = F.resize(stacked_images, [new_heights, new_widths], interpolation=interpolation)

            resized_images_grouped[shape] = stacked_images

        resized_images = reorder_images(resized_images_grouped, grouped_images_index)

        # Group images by size for further processing
        grouped_images, grouped_images_index = group_images_by_shape(resized_images)
        processed_images_grouped = {}

        for shape, stacked_images in grouped_images.items():
            # Fused rescale and normalize
            stacked_images = self.rescale_and_normalize(
                stacked_images, do_rescale, rescale_factor, do_normalize, image_mean, image_std
            )
            processed_images_grouped[shape] = stacked_images

        processed_images = reorder_images(processed_images_grouped, grouped_images_index)

        # Handle padding if required
        data = {}
        if do_pad:
            max_size = get_max_height_width(processed_images)
            padded_images = []
            pixel_masks = []

            # Create mask template for efficient masking
            if return_tensors == "pt" and len(processed_images) > 0:
                device = processed_images[0].device
                mask_template = torch.zeros(max_size, dtype=torch.int64, device=device)

            for image in processed_images:
                # Get original size
                original_size = image.shape[-2:]

                # Check if padding is needed
                if original_size[0] != max_size[0] or original_size[1] != max_size[1]:
                    padding_bottom = max_size[0] - original_size[0]
                    padding_right = max_size[1] - original_size[1]
                    padding = [0, 0, padding_right, padding_bottom]

                    # Pad the image
                    padded_image = F.pad(image, padding, fill=0)

                    # Create pixel mask (1 for valid pixels, 0 for padding)
                    pixel_mask = mask_template.clone()
                    pixel_mask[: original_size[0], : original_size[1]].fill_(1)
                else:
                    padded_image = image
                    pixel_mask = torch.ones(max_size, dtype=torch.int64, device=image.device)

                padded_images.append(padded_image)
                pixel_masks.append(pixel_mask)

            # Stack if tensors are requested
            if return_tensors == "pt":
                padded_images = torch.stack(padded_images)
                pixel_masks = torch.stack(pixel_masks)

            data["pixel_values"] = padded_images
            data["pixel_mask"] = pixel_masks
        else:
            # If no padding, just return the processed images
            if return_tensors == "pt":
                processed_images = torch.stack(processed_images)
            data["pixel_values"] = processed_images

        return BatchFeature(data=data, tensor_type=return_tensors)


__all__ = ["ViltImageProcessorFast"]
