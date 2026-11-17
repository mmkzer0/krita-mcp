![Krita × MCP](docs/banner.png)

# Krita MCP

An MCP server that lets Claude drive [Krita](https://krita.org): create
documents, build layer stacks, paint shapes and text, run filters, export
files — and *look at the canvas it just made*.

The banner above was painted by Claude through this bridge. So was
[the example poster](docs/example-poster.png). No hand-editing, no image
libraries — just tool calls.

<p align="center">
  <img src="docs/example-poster.png" alt="A sunset landscape painted in Krita through the MCP" width="700">
</p>

---

## Why it's useful

Claude can't see what it's doing in most creative tools. Here it can:
`get_image` renders the canvas and hands back a PNG, so Claude draws, looks at
the result, notices the gradient banded or the reflection looks like a
staircase, and fixes it. That loop is the whole point.

It works on real Krita documents — your layers, your colour profiles, your
`.kra` files — not a sandbox.

## Requirements

- **Krita 5.2+** (developed and tested against 5.3.3)
- **Python 3.8+** on your PATH, for the MCP server
- An MCP client — Claude Code, Claude Desktop, or anything else that speaks MCP

The MCP server uses **only the Python standard library**. There is nothing to
`pip install` and no virtualenv to manage.

## Install

### 1. Install the Krita plugin

Close Krita first — it rewrites its config on exit and would undo the enable
flag.

**Windows**

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

**Linux / macOS**

```bash
./install.sh
```

The script copies the plugin into Krita's `pykrita` folder and sets
`enable_krita_mcp=true` in `kritarc`.

Prefer to do it by hand? Copy `pykrita/krita_mcp/` and
`pykrita/krita_mcp.desktop` into Krita's resource folder under `pykrita/`, then
enable **MCP Bridge** in *Settings → Configure Krita → Python Plugin Manager*.

### 2. Start Krita and check it

```bash
python mcp_server.py --selftest
```

```
health: Krita 5.3.3, plugin 1.0.0, 31 operations
  [ok] krita_instance         5.3.3
  [ok] create_document        64x48
  [ok] pixel_channel_order    read back r=255 g=0 b=0 on a little-endian host
  [ok] pixel_roundtrip        read #0080ff
  [ok] draw                   pixel at 10,10 is #ff0000
  ...
9/9 checks passed
```

Inside Krita, *Tools → Scripts* now has **Start/Stop MCP Bridge** and
**MCP Bridge Status…**.

### 3. Connect your MCP client

**Claude Code**

```bash
claude mcp add krita -- python /absolute/path/to/mcp_server.py
```

**Claude Desktop** — add to `claude_desktop_config.json`
(`%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/` on
macOS):

```json
{
  "mcpServers": {
    "krita": {
      "command": "python",
      "args": ["/absolute/path/to/mcp_server.py"]
    }
  }
}
```

Restart the client. Krita must be running for the tools to do anything.

## Try it

> Make a 1200×800 poster in Krita: a sunset over mountains reflected in water,
> on separate layers. Show me the result when you're done.

> Open `photo.jpg`, add a 40% opacity black layer, and put the title
> "SUMMER 1998" in the bottom left. Export it as `poster.png`.

> What layers are in my open document? Set the top one to multiply at 60%.

## Tools

| Tool | What it does |
| --- | --- |
| `status` | Krita version and every open document |
| `inspect_document` | Canvas, colour space, full layer tree, selection |
| `create_document` / `open_document` | Make or load a document |
| `save_document` / `export_document` | Save in place or save-as; export to PNG/JPEG/etc. |
| `close_document` | Close a document; Krita's pending canvas work is drained first (see *Known issue*) |
| `transform_image` | resize canvas, scale, rotate, crop, flatten; the size-changing ones need a view-less document (see *Known issue*) |
| `create_layer` / `set_layer` / `delete_layer` | Layer stack management, incl. alpha lock, clipping (`clip_to_below`) and colour labels |
| `duplicate_layer` / `move_layer` / `merge_layer_down` | Restacking and merging |
| `transform_layer` | Move / scale / rotate one layer's content (undoable) |
| `get_image` | Render the canvas and return a PNG Claude can see; `grid` overlays labelled coordinates, `zoom` magnifies a region pixel-exactly |
| `get_pixel` | Exact colour at one pixel |
| `paint_strokes` | Krita's real brush engines along point lists, with pressure; any preset, undoable |
| `draw` | Exact shapes, text, gradients, bitmaps onto a paint layer |
| `add_vector_shapes` / `list_vector_shapes` / `remove_vector_shapes` | Editable text (wrapping, box fitting, outlines), speech bubbles with tails, vector shapes |
| `apply_filter` | Any Krita filter, whole layer or a region |
| `set_selection` | rect / ellipse / polygon / magic wand / colour range / layer alpha, combine modes, grow / shrink / feather / border / smooth / fill holes / invert / none |
| `fill` | Fill or erase the selection (or a region) on a paint layer |
| `checkpoint` / `restore` / `list_checkpoints` / `drop_checkpoints` | In-memory snapshot of a document's layer pixels and properties; `restore` puts them back, also on view-less documents |
| `set_layer_style` / `get_layer_style` | Outline (stroke) and drop shadow as a real Krita layer style, for SFX and lettering |
| `list_capabilities` | Filters and their parameters, blending modes, installed fonts, brush presets |
| `trigger_action` | Fire a Krita menu action by id (`edit_undo`, …) on the *active* document; script-running actions are refused while `run_python` is off (see *Arbitrary Python is opt-in*) |
| `run_python` | Arbitrary Python inside Krita, full libkis access — **off by default** |
| `self_test` | End-to-end health check |

Every tool's arguments are checked against its published schema before
anything reaches Krita: an unknown field, a wrong type or a value outside an
enum comes back as `invalid_arguments` naming the field. `null` for an optional
field means "not given".

### Arbitrary Python is opt-in

`run_python` executes whatever it is handed, with libkis and the whole standard
library in reach — the same trust level as handing over a shell. It is off in
two independent places, and both have to be on:

| Where | Switch | Default |
| --- | --- | --- |
| MCP server | `python mcp_server.py --enable-exec` | off — the tool is not advertised and calls are refused before anything reaches Krita |
| Krita plugin | `allow_python=true` under `[krita_mcp]` in `kritarc`, or start Krita with `KRITA_MCP_ALLOW_PYTHON=1` | off — the bridge refuses the operation |

```bash
# both halves for one session
KRITA_MCP_ALLOW_PYTHON=1 krita &
claude mcp add krita -- python /absolute/path/to/mcp_server.py --enable-exec
```

An explicit "off" in either place wins: `allow_python=false` in `kritarc` cannot
be overridden by an environment variable left over in a shell profile, and
`KRITA_MCP_ALLOW_PYTHON=0` still forces one session off. Enabling takes an
explicit "on" in one of the two. `/health` leaves the operation out of the list
it reports while it is off, and *Tools → Scripts → MCP Bridge Status…* names the
state and which switch decided it (`enabled (environment)`, `enabled (kritarc)`,
or `disabled`).

While the gate is closed, `trigger_action` additionally refuses the action ids
that run scripts on a stock Krita (`execute_script_1..10`, `ten_scripts`,
`python_scripter`) — without that they are a way around the switch. Treat that
list as a guard, not a sandbox: a plugin you install can register its own
script runner and the bridge cannot see it.

**Which half does what.** `--enable-exec` governs the model-facing surface: the
tool is not advertised without it, and a call is refused before any request
leaves the MCP server. The plugin setting is the enforcement point: the bridge
refuses the operation itself, so it also covers `mcp_server.py --call` and
anything else that speaks HTTP to the loopback port with the token. Neither
switch is a sandbox.

What the gate is *not*: it does not contain a process that can already read
`krita_mcp_bridge.json`, and it does not restrict file paths — `open_document`,
`save_document` and `export_document` still reach anything you can. Connect only
clients you would let act as you inside Krita.

### Drawing

`draw` takes a list of commands rendered in order onto an RGBA/8-bit paint
layer: `fill_rect`, `rect` (optional corner radius), `ellipse`, `circle`,
`line`, `polyline`, `polygon`, `text`, `linear_gradient`, `radial_gradient`,
`image` (paste a base64 PNG), and `clear` (erase to transparency). Colours
accept `#rrggbb`, `#rrggbbaa`, SVG names, or `[r,g,b,a]`.

Geometry uses `w`/`h`; outline thickness is `stroke_width`, kept separate so
the two can't be confused. Only the union of affected pixels is read, painted
and written back, so a small shape on a huge canvas stays cheap.

### Brushwork, lettering and cleanup

Three write paths, each for what it does best:

| Path | Tool | Undo | Use it for |
| --- | --- | --- | --- |
| Krita's brush engines | `paint_strokes` | yes | sketching, inking, painting — any preset, pressure per point |
| Editable vector shapes | `add_vector_shapes` | yes (Krita's shape tools) | typesetting, speech bubbles, SFX, clean line art |
| Exact pixels | `draw`, `fill` | no | pixel-exact shapes, pasting bitmaps, flat cleanup fills |

**Brushes.** `paint_strokes` takes `[[x, y], …]` (one smooth stroke through the
points) or `[[x, y, pressure], …]`. `brush` is a preset name or a unique part of
it (`list_capabilities` with `kind: brush_presets`); `size`, `color`,
`opacity` and `erase` apply to this call only and the user's settings are put
back afterwards. Krita keeps brush settings on its views, so those settings
need *some* document with a view open — it does not have to be the one being
painted; without one the call is refused with kind `needs_view`.

**Text and bubbles.** Shapes go onto a vector layer and stay editable with
Krita's text and shape tools. Coordinates are canvas pixels. A `text` shape
either sits at an `x, y` baseline (optional `width` wraps it), or fills a
`box`: it wraps to the box width, shrinks the font until the block fits
(`fit`, down to `min_size`), and aligns vertically (`valign`). The response
reports every shape's pixel bounds and the fitted size, so an agent can check
the layout without guessing. `stroke` outlines the glyphs under the fill — the
usual SFX look. A `bubble` is one seamless speech-bubble outline with a tail
pointing at `tail`. A font that is not installed is reported as a warning,
because Krita would otherwise substitute one silently.

**Cleanup (scanlation).** The usual sequence on a raw page:

```text
set_selection  mode=contiguous x,y inside the bubble   # magic wand on the white
set_selection  mode=fill_holes                          # swallow the old lettering
create_layer   name=clean
fill           layer=clean color=#ffffff                # paint it out
set_selection  mode=none
add_vector_shapes  shapes=[{type: text, box: …, align: center, font: …}]
```

`mode=color` picks every pixel near a colour (optionally inside `region`),
`layer_alpha` turns a layer's pixels into a selection (grow it and fill for a
text backing), and `combine` adds, subtracts or intersects the new pick with
the current selection.

**Seeing precisely.** `get_image` with `grid: 100` labels canvas coordinates on
the returned picture; `zoom: 8` on a small `region` returns crisp magnified
pixels. Neither changes the document.

### Addressing layers

Layers are reported in docker order, top first. Reference one by name
(`"Sky"`), path through groups (`"Background/Sky"`), index path (`"#0/#1"`), or
`"uuid:…"`. Omit it for the active layer. Listings return all of these, so
there's always an unambiguous way to name a layer.

## How it works

```
MCP client  ──stdio JSON-RPC──▶  mcp_server.py  ──HTTP──▶  Krita plugin  ──▶  libkis
                                  (stdlib only)   loopback   (GUI thread)
```

Krita only runs Python *inside* itself, and MCP servers are separate processes,
so there are two halves. They find each other through a small file in Krita's
data folder holding the port and a random per-session token. The bridge binds
loopback only and requires that token, so a web page can't drive your Krita.

**The GUI-thread rule.** libkis isn't thread-safe. The HTTP server answers on
worker threads, so every Krita call is marshalled to Krita's GUI thread through
a queued Qt signal and waited on with a deadline. If Krita's UI is blocked — a
modal dialog, a long filter — the wait ends with a clean error instead of a
hung tool call: `krita_busy` when the operation was cancelled before it started
(it did not run), or `outcome_unknown` when it had already begun and may still
finish. **Requests never hang; an operation can still be in flight.**

**A lost answer is never replayed.** If the connection breaks after a request
was sent, the client cannot know whether the operation ran, so it reports
`outcome_unknown` and leaves the decision to you rather than sending it again.
A refused connection — Krita restarted onto a new port, say — is retried once
against the discovery file, because nothing was sent the first time.

## Things worth knowing

**Pixel writes bypass undo.** `draw` and `fill` write pixels directly, which
Krita's undo history doesn't record. `paint_strokes`, `transform_layer` and
vector shapes go through Krita and are undoable. `apply_filter` on a whole
layer *is* undoable; on a sub-region it isn't, and the response says which you
got. `edit_undo` (via `trigger_action`) acts on the active document, so a
view-less document has no way to undo. Take a `checkpoint` before destructive
work and `restore` it if the result is wrong: it brings back the pixels of
RGBA/8-bit paint layers and every layer's properties, but not vector shapes,
deleted layers or the layer order.

**Blending modes can't be validated.** Krita 5.3 has no API to enumerate
composite ops and accepts any string, silently rendering unknown ids as Normal.
`set_layer` canonicalises the ids it knows and warns on anything else, so a typo
is visible rather than silent.

**Scripted documents can stay off the canvas.** `create_document` and
`open_document` take `view: false` to keep a document out of Krita's windows.
That is the mode for long automated runs, and the only mode in which the
size-changing geometry is allowed: `transform_image` with `crop`, `scale`,
`rotate` or `resize_canvas` is refused on a document a canvas is showing
(kind `unsafe_on_view`, see *Known issue*). `status` and `inspect_document`
report a `views` count, so a caller can confirm which mode it got. The default
stays `view: true` — a visible document is what makes "watch the agent paint"
work.

**Direct pixel access needs RGBA/8-bit.** `draw`, `get_pixel` and per-layer
`get_image` refuse other colour spaces with a clear message. The merged
`get_image` works regardless.

## Known issue: Krita can crash under sustained scripting

Two native faults are on record on macOS, both inside Krita's C++. The first is
refused on the operations that reach it; the second is drained around before
the teardown that would run it.

**A viewed document must not change size.** `resize_canvas`, `scale`, `rotate`
and `crop` on a document that is on a canvas can abort Krita in its OpenGL tile
cache (`ASSERT: "m_textureTiles.size() > tile"`,
`libs/ui/opengl/kis_opengl_image_textures.h:127`). The crash report from
2026-09-26 21:21:41 names the path: the update Krita coalesces for the canvas is
delivered later by a timer and rebuilds the texture cache for a document that
has already moved on.

```
KisOpenGLImageTextures::recalculateCache(KisUpdateInfo, bool)
  <- KisOpenGLCanvas2::updateCanvasProjection
  <- KisCanvas2::updateCanvasProjection
  <- KisSignalCompressor::tryEmitOnTick        (timer tick)
```

Measured on Krita 5.3.4: every view-attached soak run that executed those four
operations died — the three baseline runs after 1–5 iterations, the geometry
control in iteration 1, and four further runs on intermediate builds after 1–9
iterations — while the same revision and workload *without* them closes 90 of
90 viewed documents without a fault, and a view-less document runs the full
geometry for 3 × 30 iterations. Pumping the event loop *inside* the operation
only delivers that fatal timer sooner, so there is no plugin-side wait that
makes the edits safe. The bridge therefore refuses them on a viewed document
(kind `unsafe_on_view`) and names the workaround, which is `view: false`;
`KRITA_MCP_ALLOW_VIEWED_GEOMETRY=1` runs them anyway, for measuring the defect
rather than for normal work. `flatten` stays available on a viewed document
because it leaves the size — and with it the tile grid — alone.

A viewed document that is *not* resized is stable: it survives sitting idle,
and it closes as reliably as the close path below. Do not clear its batch mode.
A document that is on a canvas and *also* gets GUI updates dies within seconds
on this build (measured: idle death after 10s with batch mode off, no death in
120s with it on), which is why `create_document` and `open_document` leave
batch mode on for a viewed document.

**Closing a long-edited document.** `close_document` used to take Krita down
after `Document.close()` had returned, inside the settle that follows it. The
crash report for that event (2026-09-26 21:21:06) points at Krita's layer
docker: `KisNodeModel` queues raw `KisNodeDummy` pointers and hands them to its
index converter from a 100 ms coalescing timer, and nothing clears that queue
when the document's dummies are destroyed.

```
KisNodeDummy::parent()
  <- KisModelIndexConverter::indexFromDummy
  <- KisNodeModel::processUpdateQueue        (timer tick, after the teardown)
```

Draining the event loop *before* `Document.close()` lets that queued work run
while the document is still whole, and that is what `close_document` does now
(`KRITA_MCP_CLOSE_SETTLE_S` sets the budget in seconds, default 1; `0` turns it
off). Measured on this host with one build and only that switch changed: with
the drain off, three integration runs each ended in the final close with Krita
gone (`SIGSEGV`, exit status 139); with it on, five further runs completed
(`84 passed, 0 failed, 1 skipped`) and Krita stayed up. The document's age is
*not* the variable — a probe closing documents after 0, 5, 15 and 60 seconds of
the same work, viewed and view-less, closed all 24 of them cleanly.

**Prefer closing documents yourself, and keep long automation off a canvas.**
If Krita does die, nothing hangs: the next call reports "could not reach Krita",
and an operation that was in flight comes back as `outcome_unknown` rather than
being replayed.

## Troubleshooting

**"Could not reach the Krita MCP bridge"** — Krita isn't running, or the plugin
isn't enabled. Check *Tools → Scripts → MCP Bridge Status…*. If the menu entries
are missing, the plugin didn't load: enable it in the Python Plugin Manager and
restart Krita.

**Tools don't appear in the client** — restart it after editing the config, and
make sure the path to `mcp_server.py` is absolute.

**Port already in use** — the bridge scans upward from 9797 and writes whichever
port it got into the discovery file, so this normally resolves itself. Set a
different starting port with `port=` under `[krita_mcp]` in `kritarc`.

**"unsafe_on_view"** — the size-changing geometry was asked for on a document
that is on a canvas, which can take Krita down (see *Known issue*). Create or
open the document with `view: false`, or start Krita with
`KRITA_MCP_ALLOW_VIEWED_GEOMETRY=1` to run the edit anyway.

**"run_python is disabled"** — that is the default (see *Arbitrary Python is
opt-in*). A model seeing this wants both switches: `--enable-exec` on the MCP
server and `allow_python=true` in `kritarc` (or `KRITA_MCP_ALLOW_PYTHON=1`
before Krita starts).

**A `close_document` call takes about a second** — that is the drain described
under *Known issue*, and it is the point of it. `KRITA_MCP_CLOSE_SETTLE_S=0.3`
shortens the wait; `0` turns the drain off, which is how the crash it prevents
was isolated.

**Something crashed and you want to know why** — set `KRITA_MCP_TRACE=1` before
starting Krita to log every operation to `krita_mcp_trace.log` in Krita's data
folder, flushed per line. Krita is a GUI process with no console, so a trace
entry with no matching completion names the operation that killed it.

## Testing

```bash
python mcp_server.py --selftest    # quick health check inside Krita
python test_mcp.py                 # full suite, 85 checks
python test_mcp.py -v              # ...printing every tool result
python example_poster.py           # paint the example poster
python example_banner.py           # paint this README's banner
```

`test_mcp.py` spawns `mcp_server.py` and speaks real JSON-RPC to it, so a pass
covers the whole chain: protocol handshake, tool schemas, every tool against a
live Krita, pixel-exact drawing results, and the error paths. It cleans up after
itself.

Krita-free unit suites cover the parts that can be tested without a running
Krita — the HTTP client's failure handling, the GUI-thread hand-off and the
soak's own evidence handling:

```bash
python test_transport.py       # a lost answer is never replayed
python test_mainthread.py      # a job that missed its deadline is cancelled
python test_stress_close.py    # trace/crash evidence parsing
python test_gate.py            # the Python gate policy
python test_schema.py          # argument validation at the MCP boundary
python test_history.py         # checkpoint store: ids, budget, eviction
python test_style.py           # layer style ASL builder and parser
python test_raster.py          # magic wand / hole filling on raw pixels
python test_svgshapes.py       # text, bubble and shape SVG generation
```

### Soak testing

`stress_close.py` loops a heavy workload (draw, filter, geometry, save, export,
open, close) and reports what each iteration did. The default strategy closes
through the bridge and needs no Python opt-in:

```bash
python stress_close.py op 30                    # 30 iterations, view-attached
STRESS_VIEW=0 python stress_close.py op 30      # same, off-canvas documents
STRESS_GROUPS=restack,errors,reads,python python stress_close.py deferred 8
```

Every run ends with a `RESULT` line holding the run as JSON: per-iteration
outcomes, operation counts, new crash reports and the operation the trace log
left unfinished. Exit codes: 0 survived, 2 Krita died, 3 an operation failed,
4 the Python gate is closed, 5 the bridge was unreachable, 6 Krita had other
documents open (a crash would take them with it; `STRESS_ALLOW_OPEN_DOCS=1`
overrides that refusal). Keep the output: "no new crash report" is not the same
as "no crash", because macOS does not always write one.

Diagnostics:

```bash
python mcp_server.py --list-tools
python mcp_server.py --call status
python mcp_server.py --call draw --params-file commands.json
```

## Development

```powershell
powershell -ExecutionPolicy Bypass -File dev_reload.ps1
```

Copies the plugin sources over and hot-reloads `krita_mcp.ops` inside the
running Krita, so operations can be edited without restarting it. Changes to
`extension.py`, `httpserver.py` or `mainthread.py` still need a restart — those
objects already exist.

### Layout

```
mcp_server.py              MCP server (stdlib only) + CLI
install.ps1 / install.sh   install and enable the plugin
dev_reload.ps1             hot-reload during development
test_mcp.py                end-to-end suite over real JSON-RPC
stress_close.py            soak test for the close path
example_poster.py          worked example, and the client the examples share
example_banner.py          paints this README's banner
pykrita/
  krita_mcp.desktop        plugin manifest
  krita_mcp/
    __init__.py            registers the extension
    extension.py           lifetime, menu actions, settings
    httpserver.py          loopback HTTP, auth, discovery file, tracing
    mainthread.py          worker thread -> GUI thread hand-off
    ops.py                 the core operations
    paint_ops.py           brush strokes, layer transforms, selections, fill
    vector_ops.py          editable text, bubbles and shapes
    svgshapes.py           shape specs -> SVG (pure, Krita-free)
    history_ops.py         checkpoint / restore of layer pixels and properties
    style_ops.py           layer styles (stroke, drop shadow) as Krita ASL
    raster.py              magic wand / hole filling on pixel buffers (pure)
    imaging.py             QImage <-> Krita pixels, colour parsing, grid
```

### Configuration

Krita reads these from `kritarc` under `[krita_mcp]`:

| Key | Default | Meaning |
| --- | --- | --- |
| `autostart` | `true` | Start the bridge when Krita starts |
| `port` | `9797` | First port to try; scans upward if taken |

Environment variables, read by both halves:

| Variable | Meaning |
| --- | --- |
| `KRITA_MCP_STATE_DIR` | Override where the discovery file lives |
| `KRITA_MCP_TRACE` | Set to `1` before starting Krita to log every operation |
| `KRITA_MCP_PORT` / `KRITA_MCP_TOKEN` | Skip discovery and connect directly |
| `KRITA_MCP_CHECKPOINT_MB` | Plugin only: memory budget for all checkpoints together (default `1024`) |

## Platform support

Developed and tested on **Windows 10 with Krita 5.3.3**. The code paths for
Linux and macOS are implemented — resource folders, config locations, the
installer — but have not been run there. Reports welcome.

## License

[CC0 1.0 Universal](LICENSE) — public domain. Do whatever you like with it.
