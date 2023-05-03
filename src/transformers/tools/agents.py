import importlib.util
import os
import time
import warnings

import requests
from huggingface_hub import HfFolder

from .base import TASK_MAPPING, supports_remote, tool
from .prompts import CHAT_MESSAGE_PROMPT, CHAT_PROMPT_TEMPLATE, RUN_PROMPT_TEMPLATE
from .python_interpreter import evaluate


# Move to util when this branch is ready to merge
def is_openai_available():
    return importlib.util.find_spec("openai") is not None


if is_openai_available():
    import openai


BASE_PYTHON_TOOLS = {
    "print": print,
    "float": float,
    "int": int,
    "bool": bool,
    "str": str,
}

# Todo: create tools later
OUR_TOOLS = {
    "text_qa": "generative-qa",
    "image_captioner": "image-captioning",
    "image_transformer": "image-transformation",
    "text_downloader": "text-download",
    "transcriber": "speech-to-text",
    "image_generator": "image-generation",
    "text_reader": "text-to-speech",
    "text_classifier": "text-classification",
    "translator": "translation",
    "summarizer": "summarizer",
    "image_qa": "image-question-answering",
    "document_qa": "document-question-answering",
    "video_generator": "text-to-video",
    "search_engine": None,
    "database_reader": None,
    "database_writer": None,
    "table_qa": None,
}


# This is a temporary workaround for tools that aren't implemented yet.
# docstyle-ignore
MISSING_TOOLS = """
- database_reader: This is a tool that reads a record in a key-value database. It takes an input `key` and returns the value in the database.
- database_writer: This is a tool that writes a record in a key-value database. It takes an input `key` indicating the location in the database, as well as an input `value` which will populate the database. It returns the HTTP code indicating success or failure of the write operation.
- image_qa: This is a tool that answers question about images. It takes an input named `text` which should be the question in English and an input `image` which should be an image, and outputs a text that is the answer to the question.
- video_generator: This is a tool that generates a video (or animation) according to a `prompt`. The `prompt` is a text-based definition of the video to be generated. The returned value is a video object.
"""


def get_all_tools_descriptions():
    main_module = importlib.import_module("transformers")
    tools_module = main_module.tools

    lines = []
    for tool_name, task_name in OUR_TOOLS.items():
        if task_name is None:
            continue
        tool_class_name = TASK_MAPPING.get(task_name)
        description = getattr(tools_module, tool_class_name).description
        lines.append(f"- {tool_name}: {description}")

    # force-add missing tools descriptions for now
    lines.append(MISSING_TOOLS.strip())
    return "\n".join(lines)


def resolve_tools(code, remote=False):
    resolved_tools = BASE_PYTHON_TOOLS.copy()
    for name, task_name in OUR_TOOLS.items():
        if name not in code:
            continue
        if task_name is None:
            raise NotImplementedError(f"The tool {name} has not been implemented yet.")

        tool_has_remote = supports_remote(task_name)
        if remote and not tool_has_remote:
            warnings.warn(f"Loading `tool({task_name})` locally as it does not support `remote=True` yet.")
        resolved_tools[name] = tool(task_name, remote=(remote and tool_has_remote))

    return resolved_tools


def clean_code_for_chat(result):
    lines = result.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].lstrip().startswith("```"):
        idx += 1
    explanation = "\n".join(lines[:idx]).strip()
    if idx == len(lines):
        return explanation, None

    idx += 1
    start_idx = idx
    while not lines[idx].lstrip().startswith("```"):
        idx += 1
    code = "\n".join(lines[start_idx:idx]).strip()

    return explanation, code


def clean_code_for_run(result):
    result = f"I will use the following {result}"
    explanation, code = result.split("Answer:")
    explanation = explanation.strip()
    code = code.strip()

    code_lines = code.split("\n")
    if code_lines[0] in ["```", "```py"]:
        code_lines = code_lines[1:]
    if code_lines[-1] == "```":
        code_lines = code_lines[:-1]
    code = "\n".join(code_lines)

    return explanation, code


class Agent:
    chat_prompt_template = CHAT_PROMPT_TEMPLATE
    run_prompt_template = RUN_PROMPT_TEMPLATE

    def __init__(self):
        self.chat_history = None
        self.chat_state = {}

    def format_prompt(self, task):
        if getattr(self, "default_tools", None) is None:
            self.default_tools = get_all_tools_descriptions()
        prompt = self.prompt_template.replace("<<all_tools>>", self.default_tools)
        return prompt.replace("<<prompt>>", task)

    def chat(self, task, return_code=False, remote=False, **kwargs):
        if self.chat_history is None:
            prompt = CHAT_PROMPT_TEMPLATE.replace("<<all_tools>>", get_all_tools_descriptions())
        else:
            prompt = self.chat_history
        prompt += CHAT_MESSAGE_PROMPT.replace("<<task>>", task)

        result = self._generate_one(prompt, stop=["Human:", "====="])
        self.chat_history = prompt + result + "\n"
        explanation, code = clean_code_for_chat(result)

        print(f"==Explanation from the agent==\n{explanation}")

        if code is not None:
            print(f"\n\n==Code generated by the agent==\n{code}")
            if not return_code:
                print("\n\n==Result==")
                resolved_tools = resolve_tools(code, remote=remote)
                self.chat_state.update(kwargs)
                return evaluate(code, resolved_tools, self.chat_state)
            else:
                return code

    def run(self, task, return_code=False, remote=False, **kwargs):
        prompt = self.format_prompt(task)
        result = self._generate_one(prompt, stop=["Task:"])
        explanation, code = clean_code_for_run(result)

        print(f"==Explanation from the agent==\n{explanation}")

        print(f"\n\n==Code generated by the agent==\n{code}")
        if not return_code:
            print("\n\n==Result==")
            resolved_tools = resolve_tools(code, remote=remote)
            return evaluate(code, resolved_tools, kwargs)
        else:
            return code


class OpenAiAgent(Agent):
    """
    Example:

    ```py
    from transformers.tools.agents import NewOpenAiAgent

    agent = NewOpenAiAgent(model="text-davinci-003", api_key=xxx)
    agent.run("Is the following `text` (in Spanish) positive or negative?", text="¡Este es un API muy agradable!")
    ```
    """

    def __init__(self, model="gpt-3.5-turbo", api_key=None):
        if not is_openai_available():
            raise ImportError("Using `OpenAIAgent` requires `openai`: `pip install openai`.")

        if api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY", None)
        if api_key is None:
            raise ValueError(
                "You need an openai key to use `OpenAIAgent`. You can get one here: Get one here "
                "https://openai.com/api/`. If you have one, set it in your env with `os.environ['OPENAI_API_KEY'] = "
                "xxx."
            )
        else:
            openai.api_key = api_key
        self.model = model
        super().__init__()

    def generate_code(self, task):
        is_batched = isinstance(task, list)

        if is_batched:
            prompts = [self.format_prompt(one_task) for one_task in task]
        else:
            prompts = [self.format_prompt(task)]

        if "gpt" in self.model:
            results = [self._chat_generate(prompt, stop="Task:") for prompt in prompts]
        else:
            results = self._completion_generate(prompts, stop="Task:")

        return results if is_batched else results[0]

    def _generate_one(self, prompt, stop):
        if "gpt" in self.model:
            return self._chat_generate(prompt, stop)
        else:
            return self._completion_generate([prompt, stop])[0]

    def _chat_generate(self, prompt, stop):
        result = openai.ChatCompletion.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            stop=stop,
        )
        return result["choices"][0]["message"]["content"]

    def _completion_generate(self, prompts, stop):
        result = openai.Completion.create(
            model=self.model,
            prompt=prompts,
            temperature=0,
            stop=stop,
            max_tokens=200,
        )
        return [answer["text"] for answer in result["choices"]]


class EndpointAgent(Agent):
    def __init__(self, url_endpoint, token=None):
        self.url_endpoint = url_endpoint
        if token is None:
            self.token = f"Bearer {HfFolder().get_token()}"
        elif token.startswith("Bearer") or token.startswith("Basic"):
            self.token = token
        else:
            self.token = f"Bearer {token}"
        super().__init__()

    def generate_code(self, task):
        is_batched = isinstance(task, list)

        if is_batched:
            prompts = [self.format_prompt(one_task) for one_task in task]
        else:
            prompts = [self.format_prompt(task)]

        # Can probably batch those but can't test anymore right now as the endpoint has been limited in length.
        results = [self._generate_one(prompt) for prompt in prompts]
        return results if is_batched else results[0]

    def _generate_one(self, prompt, stop):
        headers = {"Authorization": self.token}
        inputs = {
            "inputs": prompt,
            "parameters": {"max_new_tokens": 200, "return_full_text": False, "stop": stop},
        }

        response = requests.post(self.url_endpoint, json=inputs, headers=headers)
        if response.status_code == 429:
            print("Getting rate-limited, waiting a tiny bit before trying again.")
            time.sleep(1)
            return self._generate_one(prompt)
        elif response.status_code != 200:
            raise ValueError(f"Error {response.status_code}: {response.json()}")

        result = response.json()[0]["generated_text"]
        # Inference API returns the stop sequence
        for stop_seq in stop:
            if result.endswith(stop_seq):
                result = result[: -len(stop_seq)]
        return result
