# Local AI Terminal Client

## 1. What It Does

This application is a feature-rich, terminal-based AI assistant client built in Python for local Large Language Models running via Ollama. It transforms local LLMs into an autonomous workspace assistant equipped with function calling, interactive safety prompts, and real-time execution capabilities.

---

## 2. Features

- **ANSI Terminal User Interface**: Provides an ANSI-colored TUI with streaming text responses, live token-per-second (TPS) calculation, custom spinner animations, markdown rendering, syntax code blocks, and ASCII tables.
- **Interactive Action Approval**: Security layer that prompts the user for confirmation prior to running shell commands, editing/writing files, or sending network API requests.
- **Dynamic Context Compression**: Monitors active token counts and conversation length to automatically condense conversation history when nearing model limits, tailored to model size.
- **Persistent Memory Storage**: Long-term key-value memory storage system backed by `memory.json` to store user preferences, facts, and instructions across sessions.
- **Session & History Management**: Save, list, load, record, and export conversation transcripts in JSON or Markdown format.
- **Enhanced Terminal Shell**: Input autocompletion for slash commands and persistent input history across restarts powered by `prompt_toolkit`.
- **Hashline Line-Level Hashing**: File reading appends 2-character MD5 hashes to line numbers (`LINE_NUM:HASH|`) allowing the agent to target precise code locations when making edits.

---

## 3. Setup

### Prerequisites
- **Python**: Version 3.10 or higher
- **Ollama**: Installed and running locally (default endpoint: `http://localhost:11434`)

### Installation Steps

1. **Install Required Packages**:
   ```bash
   pip install ollama prompt_toolkit duckduckgo_search requests beautifulsoup4 psutil
   ```

2. **Pull an Ollama Model**:
   ```bash
   ollama pull gemma4:e4b
   ```

3. **Launch the Application**:
   ```bash
   python app.py
   ```

---

## 4. Tool Capabilities

The client equips the LLM with 21 function-calling tools divided into four core domain categories:

### Web & Network Tools
- `search_web`: Conduct web searches using DuckDuckGo.
- `get_url`: Fetch web page contents and strip raw HTML down to readable text.
- `call_api`: Execute HTTP requests (GET, POST, PUT, PATCH, DELETE) with custom headers and JSON/text payloads.

### File System & Workspace Tools
- `read_file`: Read contents of a local file formatted with line numbers and line MD5 hashes.
- `write_file`: Create new files or overwrite existing file content.
- `edit_file`: Find and replace specific content snippets within an existing file.
- `delete_file`: Remove a file from disk.
- `copy_file`: Copy a file to a new location.
- `create_dir`: Create a new directory path.
- `list_dir`: Display directory contents.
- `search_in_file`: Search workspace files for string or regex patterns (grep functionality).

### System & Git Management
- `run_cmd`: Execute system shell commands (requires approval).
- `get_system_info`: Retrieve system CPU, memory usage, disk statistics, and top memory-consuming processes.
- `git_status`: Check current git repository status.
- `git_diff`: View current git working directory modifications.

### Memory & Interaction Tools
- `write_memory`: Save key information to persistent JSON storage.
- `read_memory`: Retrieve content of a specific stored memory item.
- `get_memory_list`: List stored memory IDs with timestamp and preview.
- `edit_memory`: Update content of an existing memory record.
- `delete_memory`: Remove a memory entry from disk.
- `get_user_input`: Display interactive prompt choices or ask for direct user input.

---

## 5. Slash Commands

The interactive terminal supports special slash commands to control options and inspect state:

| Command | Description |
| :--- | :--- |
| `/help` | Display the list of available commands |
| `/usage` | Render an ASCII chart of historical token consumption |
| `/model` | View active model details or open an interactive model selection menu |
| `/models` | List all locally pulled Ollama models and their disk footprints |
| `/clear` | Clear the terminal display and reset conversation history |
| `/sessions` | List saved conversation sessions |
| `/load <id>` | Load and render a past conversation session |
| `/automode <on/off>` | Enable or disable approval prompts for tool execution |
| `/fullcontent <on/off>` | Toggle truncating large file displays |
| `/record <on/off>` | Toggle automatically recording chat history into session files |
| `/export [filename]` | Export current chat history into a Markdown file |
| `/system <prompt>` | Set a custom system persona or reset to default (`/system reset`) |
| `/exit` or `/quit` | Exit the application |

---

## 6. Architecture

The codebase is organized cleanly around the following components:

- **`app.py`**: Main application engine containing the event loop, TUI output renderer, streaming logic, Ollama API integration, slash command router, context compression supervisor, and tool dispatch handler.
- **`systemprompt.py`**: System prompt builder, tool specification schemas (in JSON format), and context summarization prompt definitions.
- **`memory.json`**: Key-value JSON storage backing the long-term memory system.
- **`sessions/`**: Session directory containing JSON transcript backups for conversation history.
- **`.chat_history`**: History file managed by `prompt_toolkit` for command history recall across terminal runs.