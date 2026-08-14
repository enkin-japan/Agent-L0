import mlx.core as mx
from mlx_vlm.utils import process_image, load_audio, process_inputs_with_fallback
from PIL import Image
import numpy as np
from mlx_vlm.models.base import BaseImageProcessor


def normalize_audio_features(features: mx.array) -> mx.array:
    """mel 特征均值/方差归一化（有损音频如 MP3 的量化噪声抹平）。
    收编自 mlx_vlm 0.5.0 utils（0.6.x 上游整个移除了这步）——本栈音频链路
    （e4b 语音）是带归一化验证通过的，行为跟人不跟版本。"""
    return (features - mx.mean(features)) / (mx.std(features) + 1e-6)


def prepare_inputs(
    processor,
    images=None,
    audio=None,
    prompts=None,
    image_token_index=None,
    resize_shape=None,
    add_special_tokens=False,
    padding=True,
    padding_side="left",
    pad_to_uniform_size=False,
    return_tensors="mlx",
    **kwargs,
): 

    has_images = images is not None and (
        not hasattr(images, "__len__") or len(images) > 0
    )
    has_audio = audio is not None and (not hasattr(audio, "__len__") or len(audio) > 0)
    if not has_images and not has_audio:
        tokenizer = (
            processor.tokenizer if hasattr(processor, "tokenizer") else processor
        )
        # Ensure pad_token exists when padding text-only inputs
        if padding and tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        inputs = tokenizer(
            prompts,
            add_special_tokens=add_special_tokens,
            padding=padding,
            padding_side=padding_side,
            return_tensors=return_tensors,
        )
        input_ids = (
            inputs.input_ids
            if isinstance(inputs.input_ids, mx.array)
            else mx.array(inputs.input_ids)
        )
        mask = (
            inputs.attention_mask
            if isinstance(inputs.attention_mask, mx.array)
            else mx.array(inputs.attention_mask)
        )
        return {
            "input_ids": input_ids,
            "attention_mask": mask,
        }

    # Process images
    if images is not None:
        if not isinstance(images, list):
            images = [images]

        image_processor = (
            processor.image_processor if hasattr(processor, "image_processor") else None
        )
        images = [process_image(img, resize_shape, image_processor) for img in images]

        # For batching, we need uniform image sizes. Instead of padding to the
        # largest image (which adds white borders that hurt accuracy), we resize
        # all images to the model's expected input size.
        if len(images) > 1 and pad_to_uniform_size:
            # Get target size from image processor if available
            target_size = None
            if image_processor is not None and hasattr(image_processor, "size"):
                size = image_processor.size
                if isinstance(size, tuple):
                    target_size = size
                elif isinstance(size, dict):
                    target_size = (size.get("height", 384), size.get("width", 384))
                elif isinstance(size, int):
                    target_size = (size, size)

            if target_size is not None:
                # Resize all images to the target size
                resized_images = []
                for img in images:
                    if img.size != (
                        target_size[1],
                        target_size[0],
                    ):  # PIL uses (width, height)
                        img = img.resize(
                            (target_size[1], target_size[0]), Image.Resampling.BICUBIC
                        )
                    resized_images.append(img)
                images = resized_images
            else:
                # Fallback: pad to largest size (original behavior)
                max_width = max(img.width for img in images)
                max_height = max(img.height for img in images)

                padded_images = []
                for img in images:
                    if img.width != max_width or img.height != max_height:
                        padded_img = Image.new(
                            "RGB", (max_width, max_height), (255, 255, 255)
                        )
                        x_offset = (max_width - img.width) // 2
                        y_offset = (max_height - img.height) // 2
                        padded_img.paste(img, (x_offset, y_offset))
                        padded_images.append(padded_img)
                    else:
                        padded_images.append(img)
                images = padded_images

    # Process audio
    audio_inputs = None
    audio_feature_lengths = None
    is_qwen3_omni_moe = False
    processor_class_name = (
        processor.__class__.__name__ if hasattr(processor, "__class__") else ""
    )
    if (
        "qwen3" in processor_class_name.lower()
        and "omni" in processor_class_name.lower()
    ):
        is_qwen3_omni_moe = True

    is_lossy_audio = False
    if audio is not None and len(audio) > 0:
        if not isinstance(audio, list):
            audio = [audio]

        # Check if any audio file is a lossy format (MP3, AAC, OGG, etc.)
        lossy_extensions = {".mp3", ".m4a"}
        is_lossy_audio = any(
            str(f).lower().endswith(tuple(lossy_extensions)) for f in audio
        )
#####################################################################
# 补丁部分 开始
#####################################################################
        # if len(audio) > 1:
        #     print(
        #         "\033[33mWarning\033[0m: Single prompt with multiple audio files is not supported yet. Using the first audio file.\n"
        #     )
        #     audio = audio[:1]
#####################################################################
# 补丁部分 结束
#####################################################################
        if is_qwen3_omni_moe:
            audio_arrays = [
                load_audio(audio_file, sr=processor.feature_extractor.sampling_rate)
                for audio_file in audio
            ]
            audio_arrays = [
                audio_array.astype(np.float32) for audio_array in audio_arrays
            ]

            feature_extractor = getattr(processor, "feature_extractor", None)
            if feature_extractor is None:
                raise ValueError("Processor missing feature_extractor for audio prep.")

            audio_inputs = feature_extractor(
                audio_arrays,
                sampling_rate=feature_extractor.sampling_rate,
                padding=True,
                return_attention_mask=True,
            )

            audio_feature_lengths = np.sum(
                audio_inputs["attention_mask"], axis=-1, dtype=np.int32
            )
        else:
            feature_extractor = getattr(processor, "feature_extractor", None)
            sr = (
                getattr(feature_extractor, "sampling_rate", 16000)
                if feature_extractor is not None
                else 16000
            )
            audio = [load_audio(audio_file, sr=sr) for audio_file in audio]

    model_inputs = {}

    if hasattr(processor, "image_processor") and isinstance(
        processor.image_processor, BaseImageProcessor
    ):
        if not isinstance(prompts, list):
            prompts = [prompts]

        if processor.pad_token is None:
            processor.pad_token = processor.eos_token
        text_chunks = [
            [processor(chunk).input_ids for chunk in prompt.split("<image>")]
            for prompt in prompts
        ]

        # Find the maximum length for padding
        max_length = max(
            sum(len(chunk) for chunk in chunks) + 1 for chunks in text_chunks
        )

        # Pad and create input_ids
        input_ids = []
        for chunks in text_chunks:
            ids = chunks[0] + [image_token_index] + chunks[1]
            padding = [processor.pad_token_id] * (max_length - len(ids))
            input_ids.append(mx.array(ids + padding))

        model_inputs["input_ids"] = mx.array(input_ids)
        pixel_values = processor.image_processor.preprocess(images=images)
        model_inputs["pixel_values"] = mx.array(np.stack(pixel_values))
        model_inputs["attention_mask"] = mx.array(
            [(ids != processor.pad_token_id) for ids in input_ids]
        ).astype(mx.int32)

    else:
        if hasattr(processor, "tokenizer") and processor.tokenizer.pad_token is None:
            processor.tokenizer.pad_token = processor.tokenizer.eos_token

        inputs = process_inputs_with_fallback(
            processor,
            images=images,
            audio=audio,
            prompts=prompts,
            add_special_tokens=add_special_tokens,
            **kwargs,
        )

        if "images" in inputs:
            inputs["pixel_values"] = inputs["images"]
            inputs.pop("images")

        model_inputs["attention_mask"] = (
            mx.array(inputs["attention_mask"]) if "attention_mask" in inputs else None
        )

        # Convert inputs to model_inputs with mx.array if present
        for key, value in inputs.items():
            if key not in model_inputs:
                if value is None:
                    model_inputs[key] = value
                elif isinstance(value, (str, list, mx.array)):
                    model_inputs[key] = value
                else:
                    model_inputs[key] = mx.array(value)

    if audio_inputs is not None:
        model_inputs["input_features"] = mx.array(audio_inputs["input_features"])
        model_inputs["feature_attention_mask"] = mx.array(
            audio_inputs["attention_mask"]
        ).astype(mx.int32)
        model_inputs["audio_feature_lengths"] = mx.array(
            audio_feature_lengths, dtype=mx.int32
        )

    if is_lossy_audio and "input_features" in model_inputs:
        f = model_inputs["input_features"]
        if isinstance(f, list):
            model_inputs["input_features"] = [
                normalize_audio_features(mx.array(x)) for x in f
            ]
        else:
            model_inputs["input_features"] = normalize_audio_features(f)

    return model_inputs