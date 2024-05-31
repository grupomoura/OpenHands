import re

from agenthub.codeact_swe_agent.prompt import (
    COMMAND_DOCS,
    MINIMAL_SYSTEM_PREFIX,
    SWE_EXAMPLE,
    SYSTEM_SUFFIX,
)
from opendevin.controller.agent import Agent
from opendevin.controller.state.state import State
from opendevin.events.action import (
    Action,
    AgentFinishAction,
    BrowseInteractiveAction,
    CmdRunAction,
    IPythonRunCellAction,
    MessageAction,
)
from opendevin.events.observation import (
    BrowserOutputObservation,
    CmdOutputObservation,
    IPythonRunCellObservation,
)
from opendevin.llm.llm import LLM
from opendevin.runtime.plugins import (
    AgentSkillsRequirement,
    JupyterRequirement,
    PluginRequirement,
)


def parse_response(response) -> str:
    action = response.choices[0].message.content
    for lang in ['bash', 'ipython', 'browse']:
        if f'<execute_{lang}>' in action and f'</execute_{lang}>' not in action:
            action += f'</execute_{lang}>'
    return action


def action_to_str(action: Action) -> str:
    if isinstance(action, CmdRunAction):
        return f'{action.thought}\n<execute_bash>\n{action.command}\n</execute_bash>'
    elif isinstance(action, IPythonRunCellAction):
        return f'{action.thought}\n<execute_ipython>\n{action.code}\n</execute_ipython>'
    elif isinstance(action, BrowseInteractiveAction):
        return f'{action.thought}\n<execute_browse>\n{action.browser_actions}\n</execute_browse>'
    elif isinstance(action, MessageAction):
        return action.content
    return ''


def get_action_message(action: Action) -> dict[str, str] | None:
    if (
        isinstance(action, BrowseInteractiveAction)
        or isinstance(action, CmdRunAction)
        or isinstance(action, IPythonRunCellAction)
        or isinstance(action, MessageAction)
    ):
        return {
            'role': 'user' if action.source == 'user' else 'assistant',
            'content': action_to_str(action),
        }
    return None


def get_observation_message(obs) -> dict[str, str] | None:
    if isinstance(obs, CmdOutputObservation):
        content = 'OBSERVATION:\n' + truncate_observation(obs.content)
        content += (
            f'\n[Command {obs.command_id} finished with exit code {obs.exit_code}]]'
        )
        return {'role': 'user', 'content': content}
    elif isinstance(obs, IPythonRunCellObservation):
        content = 'OBSERVATION:\n' + obs.content
        # replace base64 images with a placeholder
        splitted = content.split('\n')
        for i, line in enumerate(splitted):
            if '![image](data:image/png;base64,' in line:
                splitted[i] = (
                    '![image](data:image/png;base64, ...) already displayed to user'
                )
        content = '\n'.join(splitted)
        content = truncate_observation(content)
        return {'role': 'user', 'content': content}
    elif isinstance(obs, BrowserOutputObservation):
        content = 'OBSERVATION:\n' + truncate_observation(obs.content)
        return {'role': 'user', 'content': content}
    return None


def truncate_observation(observation: str, max_chars: int = 10_000) -> str:
    """
    Truncate the middle of the observation if it is too long.
    """
    if len(observation) <= max_chars:
        return observation
    half = max_chars // 2
    return (
        observation[:half]
        + '\n[... Observation truncated due to length ...]\n'
        + observation[-half:]
    )


def get_system_message() -> str:
    return f'{MINIMAL_SYSTEM_PREFIX}\n\n{COMMAND_DOCS}\n\n{SYSTEM_SUFFIX}'


def get_in_context_example() -> str:
    return SWE_EXAMPLE


class CodeActSWEAgent(Agent):
    VERSION = '1.5'
    """
    This agent is an adaptation of the original [SWE Agent](https://swe-agent.com/) based on CodeAct 1.5 using the `agentskills` library of OpenDevin.

    It is intended use is **solving Github issues**.

    It removes web-browsing and Github capability from the original CodeAct agent to avoid confusion to the agent.
    """

    sandbox_plugins: list[PluginRequirement] = [
        # NOTE: AgentSkillsRequirement need to go before JupyterRequirement, since
        # AgentSkillsRequirement provides a lot of Python functions
        # and it need to be initialized before Jupyter for Jupyter to use those functions.
        AgentSkillsRequirement(),
        JupyterRequirement(),
    ]
    jupyter_kernel_init_code: str = 'from agentskills import *'

    system_message: str = get_system_message()
    in_context_example: str = f"Here is an example of how you can interact with the environment for task solving:\n{get_in_context_example()}\n\nNOW, LET'S START!"

    def __init__(
        self,
        llm: LLM,
    ) -> None:
        """
        Initializes a new instance of the CodeActAgent class.

        Parameters:
        - llm (LLM): The llm to be used by this agent
        """
        super().__init__(llm)
        self.reset()

    def reset(self) -> None:
        """
        Resets the CodeAct Agent.
        """
        super().reset()

    def step(self, state: State) -> Action:
        """
        Performs one step using the CodeAct Agent.
        This includes gathering info on previous steps and prompting the model to make a command to execute.

        Parameters:
        - state (State): used to get updated info and background commands

        Returns:
        - CmdRunAction(command) - bash command to run
        - IPythonRunCellAction(code) - IPython code to run
        - BrowseInteractiveAction(browsergym_command) - BrowserGym commands to run
        - MessageAction(content) - Message action to run (e.g. ask for clarification)
        - AgentFinishAction() - end the interaction
        """
        messages: list[dict[str, str]] = self._get_messages(state)

        latest_user_message = next(
            (m for m in reversed(messages) if m['role'] == 'user'), None
        )
        if latest_user_message and latest_user_message['content'].strip() == '/exit':
            return AgentFinishAction()

        response = self.llm.completion(
            messages=messages,
            stop=[
                '</execute_ipython>',
                '</execute_bash>',
                '</execute_browse>',
            ],
            temperature=0.0,
        )

        action_str: str = parse_response(response)
        state.num_of_chars += sum(
            len(message['content']) for message in messages
        ) + len(action_str)

        if finish_command := re.search(r'<finish>.*</finish>', action_str, re.DOTALL):
            thought = action_str.replace(finish_command.group(0), '').strip()
            return AgentFinishAction(thought=thought)
        if bash_command := re.search(
            r'<execute_bash>(.*?)</execute_bash>', action_str, re.DOTALL
        ):
            # remove the command from the action string to get thought
            thought = action_str.replace(bash_command.group(0), '').strip()
            # a command was found
            command_group = bash_command.group(1).strip()

            if command_group.strip() == 'exit':
                return AgentFinishAction()
            return CmdRunAction(command=command_group, thought=thought)
        elif python_code := re.search(
            r'<execute_ipython>(.*?)</execute_ipython>', action_str, re.DOTALL
        ):
            # a code block was found
            code_group = python_code.group(1).strip()
            thought = action_str.replace(python_code.group(0), '').strip()
            return IPythonRunCellAction(
                code=code_group,
                thought=thought,
                kernel_init_code=self.jupyter_kernel_init_code,
            )
        elif browse_command := re.search(
            r'<execute_browse>(.*)</execute_browse>', action_str, re.DOTALL
        ):
            # BrowserGym actions was found
            browse_actions = browse_command.group(1).strip()
            thought = action_str.replace(browse_command.group(0), '').strip()
            return BrowseInteractiveAction(
                browser_actions=browse_actions, thought=thought
            )
        else:
            # We assume the LLM is GOOD enough that when it returns pure natural language
            # it want to talk to the user
            return MessageAction(content=action_str, wait_for_response=True)

    def search_memory(self, query: str) -> list[str]:
        raise NotImplementedError('Implement this abstract method')

    def _get_messages(self, state: State) -> list[dict[str, str]]:
        messages = [
            {'role': 'system', 'content': self.system_message},
            {
                'role': 'user',
                'content': self.in_context_example,
            },
        ]

        for event in state.history:
            message = (
                get_action_message(event)
                if isinstance(event, Action)
                else get_observation_message(event)
            )
            if message:
                messages.append(message)

        latest_user_message = latest_user_message = next(
            (m for m in reversed(messages) if m['role'] == 'user'), None
        )
        if latest_user_message:
            if latest_user_message['content'].strip() == '/exit':
                return messages
            latest_user_message['content'] += (
                f'\n\nENVIRONMENT REMINDER: You have {state.max_iterations - state.iteration} turns left to complete the task.'
            )

        return messages
