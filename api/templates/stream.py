from __future__ import annotations

import gc
import time
import uuid
from threading import Thread
from types import MethodType
from typing import (
    Dict,
    Any,
    TYPE_CHECKING,
    Iterable,
)

import torch
from transformers import TextIteratorStreamer
from transformers.generation.logits_process import (
    LogitsProcessorList,
    RepetitionPenaltyLogitsProcessor,
    TemperatureLogitsWarper,
    TopKLogitsWarper,
    TopPLogitsWarper,
)

from api.templates.utils import apply_stopping_strings

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizer, PreTrainedModel


@torch.inference_mode()
def generate_stream(
    model: "PreTrainedModel",
    tokenizer: "PreTrainedTokenizer",
    params: Dict[str, Any],
):
    inputs = params.get("inputs")
    functions = params.get("functions")
    model_name = params.get("model", "llm")
    temperature = float(params.get("temperature", 1.0))
    repetition_penalty = float(params.get("repetition_penalty", 1.0))
    top_p = float(params.get("top_p", 1.0))
    top_k = int(params.get("top_k", 50))
    max_new_tokens = int(params.get("max_tokens", 256))

    stop_token_ids = params.get("stop_token_ids") or []
    if tokenizer.eos_token_id not in stop_token_ids:
        stop_token_ids.append(tokenizer.eos_token_id)
    stop_strings = params.get("stop", [])

    device = next(model.parameters()).device
    generation_kwargs = dict(
        do_sample=True,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        max_new_tokens=max_new_tokens,
        repetition_penalty=repetition_penalty,
        pad_token_id=tokenizer.pad_token_id,
    )
    if temperature <= 1e-5:
        generation_kwargs["do_sample"] = False
        generation_kwargs.pop("top_k")

    if isinstance(inputs, dict):
        inputs = {k: v.to(device) for k, v in inputs.items() if isinstance(v, torch.Tensor)}
        generation_kwargs.update(inputs)
        input_echo_len = len(inputs["input_ids"][0])
    else:
        generation_kwargs["input_ids"] = torch.tensor([inputs], device=device)
        input_echo_len = len(inputs)

    streamer = TextIteratorStreamer(
        tokenizer, timeout=60.0, skip_prompt=True, skip_special_tokens=True
    )
    generation_kwargs["streamer"] = streamer

    if "GenerationMixin" not in str(model.generate.__func__):
        model.generate = MethodType(PreTrainedModel.generate, model)

    thread = Thread(target=model.generate, kwargs=generation_kwargs)
    thread.start()

    generated_text, func_call_found = "", False
    completion_id: str = f"cmpl-{str(uuid.uuid4())}"
    created: int = int(time.time())
    previous_text = ""
    for i, new_text in enumerate(streamer):
        generated_text += new_text
        if functions:
            _, func_call_found = apply_stopping_strings(generated_text, ["Observation:"])
        generated_text, stop_found = apply_stopping_strings(generated_text, stop_strings)

        if generated_text and generated_text[-1] != "�":
            delta_text = generated_text[len(previous_text):]
            previous_text = generated_text

            yield {
                "id": completion_id,
                "object": "text_completion",
                "created": created,
                "model": model_name,
                "delta": delta_text,
                "text": generated_text,
                "logprobs": None,
                "finish_reason": "function_call" if func_call_found else None,
                "usage": {
                    "prompt_tokens": input_echo_len,
                    "completion_tokens": i,
                    "total_tokens": input_echo_len + i,
                },
            }

        if stop_found:
            break

    yield {
        "id": completion_id,
        "object": "text_completion",
        "created": created,
        "model": model_name,
        "delta": "",
        "text": generated_text,
        "logprobs": None,
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": input_echo_len,
            "completion_tokens": i,
            "total_tokens": input_echo_len + i,
        },
    }

    gc.collect()
    torch.cuda.empty_cache()


def prepare_logits_processor(
    temperature: float, repetition_penalty: float, top_p: float, top_k: int
) -> LogitsProcessorList:
    """
    Prepare a list of logits processors based on the provided parameters.

    Args:
        temperature (float): The temperature value for temperature warping.
        repetition_penalty (float): The repetition penalty value.
        top_p (float): The top-p value for top-p warping.
        top_k (int): The top-k value for top-k warping.

    Returns:
        LogitsProcessorList: A list of logits processors.
    """
    processor_list = LogitsProcessorList()
    # TemperatureLogitsWarper doesn't accept 0.0, 1.0 makes it a no-op, so we skip two cases.
    if temperature >= 1e-5 and temperature != 1.0:
        processor_list.append(TemperatureLogitsWarper(temperature))
    if repetition_penalty > 1.0:
        processor_list.append(RepetitionPenaltyLogitsProcessor(repetition_penalty))
    if 1e-8 <= top_p < 1.0:
        processor_list.append(TopPLogitsWarper(top_p))
    if top_k > 0:
        processor_list.append(TopKLogitsWarper(top_k))
    return processor_list


def is_partial_stop(output: str, stop_str: str):
    """ Check whether the output contains a partial stop str. """
    return any(
        stop_str.startswith(output[-i:])
        for i in range(0, min(len(output), len(stop_str)))
    )


@torch.inference_mode()
def generate_stream_old(
    model: "PreTrainedModel",
    tokenizer: "PreTrainedTokenizer",
    params: Dict[str, Any],
):
    # Read parameters
    input_ids = params.get("inputs")
    prompt = params.get("prompt")
    model_name = params.get("model", "llm")
    temperature = float(params.get("temperature", 1.0))
    repetition_penalty = float(params.get("repetition_penalty", 1.0))
    top_p = float(params.get("top_p", 1.0))
    top_k = int(params.get("top_k", -1))  # -1 means disable
    max_new_tokens = int(params.get("max_tokens", 256))
    echo = bool(params.get("echo", True))
    stop_str = params.get("stop")

    stop_token_ids = params.get("stop_token_ids") or []
    if tokenizer.eos_token_id not in stop_token_ids:
        stop_token_ids.append(tokenizer.eos_token_id)

    logits_processor = prepare_logits_processor(
        temperature, repetition_penalty, top_p, top_k
    )

    output_ids = list(input_ids)
    input_echo_len = len(input_ids)

    device = next(model.parameters()).device
    start_ids = torch.as_tensor([input_ids], device=device)

    past_key_values, sent_interrupt = None, False
    completion_id: str = f"cmpl-{str(uuid.uuid4())}"
    created: int = int(time.time())
    previous_text = ""
    for i in range(max_new_tokens):
        if i == 0:  # prefill
            out = model(input_ids=start_ids, use_cache=True)
            logits = out.logits
            past_key_values = out.past_key_values
        else:  # decoding
            out = model(
                input_ids=torch.as_tensor(
                    [[token] if not sent_interrupt else output_ids],
                    device=device,
                ),
                use_cache=True,
                past_key_values=past_key_values if not sent_interrupt else None,
            )
            sent_interrupt = False
            logits = out.logits
            past_key_values = out.past_key_values

        if logits_processor:
            if repetition_penalty > 1.0:
                tmp_output_ids = torch.as_tensor([output_ids], device=logits.device)
            else:
                tmp_output_ids = None
            last_token_logits = logits_processor(tmp_output_ids, logits[:, -1, :])[0]
        else:
            last_token_logits = logits[0, -1, :]

        if device == "mps":
            # Switch to CPU by avoiding some bugs in mps backend.
            last_token_logits = last_token_logits.float().to("cpu")

        if temperature < 1e-5 or top_p < 1e-8:  # greedy
            _, indices = torch.topk(last_token_logits, 2)
            tokens = [int(index) for index in indices.tolist()]
        else:
            probs = torch.softmax(last_token_logits, dim=-1)
            indices = torch.multinomial(probs, num_samples=2)
            tokens = [int(token) for token in indices.tolist()]

        token = tokens[0]
        output_ids.append(token)

        if token in stop_token_ids:
            stopped = True
        else:
            stopped = False

        # Yield the output tokens
        if i % 2 == 0 or i == max_new_tokens - 1 or stopped:
            if echo:
                tmp_output_ids = output_ids
                rfind_start = len(prompt)
            else:
                tmp_output_ids = output_ids[input_echo_len:]
                rfind_start = 0

            output = tokenizer.decode(
                tmp_output_ids,
                skip_special_tokens=True,
                spaces_between_special_tokens=False,
                clean_up_tokenization_spaces=True,
            )

            partially_stopped, finish_reason = False, None
            if stop_str:
                if isinstance(stop_str, str):
                    pos = output.rfind(stop_str, rfind_start)
                    if pos != -1:
                        output = output[:pos]
                        stopped = True
                    else:
                        partially_stopped = is_partial_stop(output, stop_str)
                elif isinstance(stop_str, Iterable):
                    for each_stop in stop_str:
                        pos = output.rfind(each_stop, rfind_start)
                        if pos != -1:
                            output = output[:pos]
                            stopped = True
                            if each_stop == "Observation:":
                                finish_reason = "function_call"
                            break
                        else:
                            partially_stopped = is_partial_stop(output, each_stop)
                            if partially_stopped:
                                break
                else:
                    raise ValueError("Invalid stop field type.")

            # Prevent yielding partial stop sequence
            if (not partially_stopped) and output and output[-1] != "�":
                delta_text = output[len(previous_text):]
                previous_text = output

                yield {
                    "id": completion_id,
                    "object": "text_completion",
                    "created": created,
                    "model": model_name,
                    "delta": delta_text,
                    "text": output,
                    "logprobs": None,
                    "finish_reason": finish_reason,
                    "usage": {
                        "prompt_tokens": input_echo_len,
                        "completion_tokens": i,
                        "total_tokens": input_echo_len + i,
                    },
                }

        if stopped:
            break

    yield {
        "id": completion_id,
        "object": "text_completion",
        "created": created,
        "model": model_name,
        "delta": "",
        "text": output,
        "logprobs": None,
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": input_echo_len,
            "completion_tokens": i,
            "total_tokens": input_echo_len + i,
        },
    }

    # Clean
    del past_key_values, out
    gc.collect()
    torch.cuda.empty_cache()
