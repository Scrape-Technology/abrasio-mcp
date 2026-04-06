# abrasio-mcp

MCP server that exposes **Abrasio** as an agentic browser for AI models.

Allows Claude, Cursor, and any MCP-compatible AI to control a real web browser with full anti-detection fingerprinting — the same stealth infrastructure used for web scraping, now driven by AI.

---

## How it works

```
AI Model (Claude, Cursor, etc.)
    │  MCP tool calls
    ▼
abrasio-mcp server
    │  Abrasio SDK
    ▼
Patchright (undetected Playwright fork)
    │  CDP WebSocket
    ▼
Chrome (local stealth)  OR  Abrasio cloud worker (fingerprinted, residential IP)
```

The MCP server holds a single persistent browser session. The AI calls tools to navigate, observe, and interact — no configuration needed between steps.

---

## Installation

```bash
pip install abrasio-mcp
```

Or install from source:

```bash
cd abrasio-mcp
pip install -e .
```

---

## Modes

### Local mode (free)

No API key required. Launches Chrome on your machine with Patchright stealth patches.

```bash
abrasio-mcp
```

Best for: development, testing, scraping sites that don't require real residential IPs.

### Cloud mode (paid)

Requires an Abrasio API key. The browser runs on Abrasio cloud infrastructure with:
- Real collected browser fingerprints (not spoofed)
- Residential or datacenter IP in the target region
- Persistent profiles that accumulate browser history

```bash
ABRASIO_API_KEY=sk_live_xxx abrasio-mcp
```

Best for: production agents, geo-targeted tasks, heavily protected sites.

---

## Configuration

All configuration is done via environment variables. No config files required.

| Variable | Default | Description |
|---|---|---|
| `ABRASIO_API_KEY` | _(none)_ | Cloud API key. If set, enables cloud mode. If unset, uses local mode. |
| `ABRASIO_HEADLESS` | `true` | Run browser headless. Set `false` to see the browser window (local mode only). |
| `ABRASIO_REGION` | _(none)_ | Target region for geo-configuration, e.g. `BR`, `US`, `DE`. Configures locale and timezone automatically. |
| `ABRASIO_HUMANIZE` | `false` | Set `true` to enable human-like interaction timing on all actions (slower but more realistic). |
| `ABRASIO_TRANSPORT` | `stdio` | MCP transport: `stdio` for local clients, `streamable-http` for remote/production. |
| `ABRASIO_HOST` | `127.0.0.1` | Bind host when using `streamable-http` transport. |
| `ABRASIO_PORT` | `8931` | Bind port when using `streamable-http` transport. |

---

## Integrations

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "abrasio": {
      "command": "abrasio-mcp",
      "env": {
        "ABRASIO_API_KEY": "sk_live_xxx",
        "ABRASIO_REGION": "BR"
      }
    }
  }
}
```

For local mode (no API key):

```json
{
  "mcpServers": {
    "abrasio": {
      "command": "abrasio-mcp",
      "env": {
        "ABRASIO_HEADLESS": "false"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add abrasio -- abrasio-mcp
```

With environment variables:

```bash
claude mcp add abrasio -e ABRASIO_API_KEY=sk_live_xxx -e ABRASIO_REGION=BR -- abrasio-mcp
```

### Cursor

Add to `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "abrasio": {
      "command": "abrasio-mcp",
      "env": {
        "ABRASIO_API_KEY": "sk_live_xxx"
      }
    }
  }
}
```

### Streamable HTTP (production / remote)

Start the server:

```bash
ABRASIO_TRANSPORT=streamable-http \
ABRASIO_HOST=0.0.0.0 \
ABRASIO_PORT=8931 \
ABRASIO_API_KEY=sk_live_xxx \
abrasio-mcp
```

Configure the client to connect to `http://your-server:8931/mcp`.

---

## Tools reference

The MCP server exposes 15 tools, organized in four groups.

---

### Navigation

#### `browser_navigate`

Navigate to a URL and wait for the page to load.

| Parameter | Type | Description |
|---|---|---|
| `url` | `string` | Full URL including scheme, e.g. `https://example.com` |

**Returns:** JSON object with `url` (final URL after redirects), `title`, and `status` (HTTP status code).

```json
{ "url": "https://example.com/home", "title": "Home — Example", "status": 200 }
```

---

#### `browser_go_back`

Go back to the previous page in browser history.

**Returns:** JSON object with `url` and `title` of the page navigated to.

---

#### `browser_go_forward`

Go forward to the next page in browser history.

**Returns:** JSON object with `url` and `title`.

---

#### `browser_reload`

Reload the current page.

**Returns:** JSON object with `url` and `title`.

---

#### `browser_get_url`

Get the current page URL.

**Returns:** String with the current URL.

---

### Observation

#### `browser_screenshot`

Take a screenshot of the current page.

**Returns:** PNG image. Claude can see this image and use it to understand the visual state of the browser before deciding what to interact with.

> Use this at the start of a task and after each major interaction to confirm the expected result.

---

#### `browser_get_text`

Extract all visible text from the current page body.

**Returns:** Plain text string. Useful for reading articles, prices, search results, or any text-based content without parsing HTML.

---

#### `browser_get_html`

Get the inner HTML of an element.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `selector` | `string` | `body` | CSS selector for the target element |

**Returns:** HTML string of the matched element.

---

#### `browser_find_elements`

Find all interactive elements on the page.

**Returns:** JSON array. Each item represents a clickable, fillable, or otherwise interactive element:

```json
[
  {
    "tag": "button",
    "type": "submit",
    "text": "Sign in",
    "href": null,
    "selector": "button.login-btn",
    "x": 720,
    "y": 412,
    "visible": true
  },
  {
    "tag": "input",
    "type": "email",
    "text": "",
    "href": null,
    "selector": "input[name=\"email\"]",
    "x": 720,
    "y": 340,
    "visible": true
  }
]
```

| Field | Description |
|---|---|
| `tag` | HTML tag name |
| `type` | Input type (`text`, `email`, `submit`, etc.) or `null` |
| `text` | Visible text, value, placeholder, or aria-label (up to 120 chars) |
| `href` | Link URL for anchor elements |
| `selector` | CSS selector hint — use this with `browser_click` and `browser_fill` |
| `x`, `y` | Center coordinates in the viewport |
| `visible` | Whether the element is within the current viewport |

> Call `browser_find_elements` before interacting to get the correct selector for `browser_click` and `browser_fill`.

---

#### `browser_wait_for`

Wait for an element to appear in the DOM.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `selector` | `string` | _(required)_ | CSS selector to wait for |
| `timeout` | `integer` | `10000` | Maximum wait time in milliseconds |

**Returns:** JSON object with `found: true`, the `selector`, and the element's `text` content.

Use this after triggering actions that cause loading states (form submissions, route changes, AJAX updates).

---

### Interaction

All interaction tools use the Abrasio human simulation layer:
- **Clicks** use WindMouse — a physics-based cursor movement algorithm that mimics real hand movement with gravity, wind perturbations, and velocity management.
- **Typing** uses character-level timing with realistic delays, burst typing, and occasional typos followed by backspace correction.
- **Scrolling** uses a 3-phase easing curve (acceleration → constant → deceleration).

---

#### `browser_click`

Click an element by CSS selector.

| Parameter | Type | Description |
|---|---|---|
| `selector` | `string` | CSS selector for the element to click |

**Returns:** Confirmation string.

```
Clicked: button#submit
```

---

#### `browser_fill`

Click a form input and fill it with text.

| Parameter | Type | Description |
|---|---|---|
| `selector` | `string` | CSS selector for the input element |
| `text` | `string` | Value to type into the field |

Clears any existing value, clicks the field, then types with human-like timing.

**Returns:** Confirmation string with character count.

> Prefer `browser_fill` over `browser_type` when targeting a specific input. Use `browser_type` only when the input is already focused.

---

#### `browser_type`

Type text at the current cursor position.

| Parameter | Type | Description |
|---|---|---|
| `text` | `string` | Text to type |

Types into whichever element currently has focus. Useful for keyboard shortcuts, search boxes that open on click, or multi-step typing flows.

**Returns:** Confirmation string with character count.

---

#### `browser_scroll`

Scroll the page vertically.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pixels` | `integer` | `800` | Pixels to scroll. Positive = down, negative = up. |

**Returns:** Confirmation string indicating direction and distance.

---

#### `browser_hover`

Move the mouse over an element without clicking.

| Parameter | Type | Description |
|---|---|---|
| `selector` | `string` | CSS selector for the element to hover |

Uses WindMouse movement to reach the element. Useful for revealing dropdown menus, tooltips, or hover-triggered UI elements.

**Returns:** Confirmation string.

---

#### `browser_press`

Press a keyboard key or key combination.

| Parameter | Type | Description |
|---|---|---|
| `key` | `string` | Key name or combination |

Common values:

| Key string | Action |
|---|---|
| `Enter` | Submit form / confirm |
| `Tab` | Move focus to next field |
| `Escape` | Close modal / cancel |
| `ArrowDown` / `ArrowUp` | Navigate dropdowns |
| `Control+a` | Select all |
| `Control+c` | Copy |
| `Meta+Return` | Submit (macOS) |

**Returns:** Confirmation string.

---

### Evaluation

#### `browser_evaluate`

Execute JavaScript in the current page context.

| Parameter | Type | Description |
|---|---|---|
| `script` | `string` | JavaScript expression or function |

The script can be a simple expression or a function that returns a value:

```js
// Expression
document.title

// Arrow function
() => document.querySelectorAll('h2').length

// Function with logic
() => {
  const el = document.querySelector('#price');
  return el ? el.innerText.trim() : null;
}

// Read localStorage
() => ({ token: localStorage.getItem('auth_token') })
```

**Returns:** JSON-serialized result of the script execution.

> Use this for data extraction that `browser_get_text` and `browser_get_html` cannot handle — structured data, counts, computed values, or interacting with the page's JavaScript environment.

---

## Patterns and best practices

### Starting a task

Always begin with a screenshot to understand the current state:

```
1. browser_navigate("https://target.com")
2. browser_screenshot()           ← see what loaded
3. browser_find_elements()        ← discover available interactions
```

### Filling a login form

```
1. browser_navigate("https://site.com/login")
2. browser_screenshot()
3. browser_find_elements()        ← get selectors for email/password fields
4. browser_fill("input[name='email']", "user@example.com")
5. browser_fill("input[name='password']", "secret")
6. browser_click("button[type='submit']")
7. browser_wait_for(".dashboard")  ← wait for redirect
8. browser_screenshot()            ← confirm login succeeded
```

### Handling dynamic content

```
1. browser_click(".load-more-btn")
2. browser_wait_for(".new-items-loaded")   ← wait for content
3. browser_get_text()                      ← extract updated content
```

### Extracting structured data

```
1. browser_navigate("https://shop.com/product/123")
2. browser_evaluate("() => ({ name: document.querySelector('h1').innerText, price: document.querySelector('.price').innerText })")
```

### Navigating paginated results

```
1. browser_navigate("https://site.com/results?page=1")
2. browser_get_text()
3. browser_click("a[aria-label='Next page']")
4. browser_wait_for(".results")
5. browser_get_text()
```

---

## Architecture

```
abrasio-mcp/
├── pyproject.toml
└── abrasio_mcp/
    ├── __init__.py
    ├── server.py          # FastMCP server, tool registration, entry point
    ├── browser.py         # AbrasioBrowserAgent — wraps Abrasio SDK + Page
    └── tools/
        ├── __init__.py
        ├── navigate.py    # browser_navigate, go_back, go_forward, reload, get_url
        ├── observe.py     # browser_screenshot, get_text, get_html, find_elements, wait_for
        ├── interact.py    # browser_click, fill, type, scroll, hover, press
        └── evaluate.py    # browser_evaluate
```

### Session lifecycle

The browser session is lazy-started: the MCP server process starts immediately, and the browser only opens when the first tool is called. The session persists for the lifetime of the server process — navigation history, cookies, and localStorage are preserved across all tool calls.

On SIGTERM or SIGINT, the server calls `Abrasio.close()` which signals the worker to stop (in cloud mode) before dropping the CDP connection, ensuring proper billing finalization.

### Browser agent (`browser.py`)

`AbrasioBrowserAgent` wraps `Abrasio` (the SDK's unified class) and holds a single `Page` object. All tools delegate to this agent. The `asyncio.Lock` on `ensure_started` prevents concurrent initialization if two tools are called in rapid succession before the browser is ready.

All interaction methods use the `human/actions.py` primitives from the Abrasio SDK directly — no custom mouse or keyboard simulation is implemented in `abrasio-mcp`.

---

## Requirements

- Python 3.10+
- `mcp >= 1.0.0`
- `abrasio >= 0.1.2` (installs `patchright` automatically)
- For cloud mode: an Abrasio API key (`sk_live_...`)

---

## License

MIT — Scrape Technology
