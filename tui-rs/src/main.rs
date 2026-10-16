mod app;
mod theme;
mod ui;

use crossterm::event::{
    DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture, Event,
    KeyEventKind, KeyboardEnhancementFlags, PopKeyboardEnhancementFlags,
    PushKeyboardEnhancementFlags,
};
use crossterm::{execute, terminal::*};
use ratatui::{backend::CrosstermBackend, Terminal};
use serde::{Deserialize, Serialize};
use std::io::Write;
use tokio::io::AsyncBufReadExt;
use tokio::sync::mpsc;

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "type")]
enum PyMsg {
    #[serde(rename = "add_message")]
    AddMessage { role: String, content: String },
    #[serde(rename = "update")]
    Update { sidebar: Sidebar },
    #[serde(rename = "set_working")]
    SetWorking { working: bool, message: String },
    #[serde(rename = "clear")]
    Clear,
    #[serde(rename = "prompt_select")]
    PromptSelect { title: String, items: Vec<String> },
    #[serde(rename = "set_completions")]
    SetCompletions { items: Vec<String> },
    #[serde(rename = "system")]
    System { content: String },
    #[serde(rename = "quit")]
    Quit,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct Sidebar {
    #[serde(default)]
    mode: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    tokens_used: u64,
    #[serde(default)]
    tokens_budget: u64,
    #[serde(default)]
    cwd: String,
    #[serde(default)]
    working_message: String,
    #[serde(default)]
    is_working: bool,
    #[serde(default)]
    todos: Vec<Todo>,
    #[serde(default)]
    subagents: Vec<Sub>,
    #[serde(default)]
    procs: Vec<Proc>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct Todo {
    text: String,
    done: bool,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
struct Sub {
    role: String,
    status: String,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
struct Proc {
    command: String,
    pid: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type")]
enum RustMsg {
    #[serde(rename = "input")]
    Input { text: String },
    #[serde(rename = "select_result")]
    SelectResult { index: Option<usize> },
    #[serde(rename = "mode_cycle")]
    ModeCycle { mode: String },
    #[serde(rename = "interrupt")]
    Interrupt,
    #[serde(rename = "eof")]
    Eof,
}

impl Default for Sidebar {
    fn default() -> Self {
        Sidebar {
            mode: "Build Mode".into(),
            model: "unknown".into(),
            tokens_used: 0,
            tokens_budget: 128000,
            cwd: "~".into(),
            working_message: String::new(),
            is_working: false,
            todos: vec![],
            subagents: vec![],
            procs: vec![],
        }
    }
}

type R = Result<(), Box<dyn std::error::Error>>;

/// Incoming NDJSON lines longer than this are dropped (protects memory).
const MAX_LINE: usize = 1 << 20;

// Protocol channel: NDJSON over a TCP loopback socket (bridge mode,
// NIM_TUI_PORT) or stdio (direct `--demo`/pipe use). In stdio mode stdout is
// the screen, so outgoing messages go to stderr instead of drawing over it.
enum Channel {
    Stdio,
    Socket(tokio::net::tcp::OwnedWriteHalf),
}

impl Channel {
    async fn emit(&mut self, msg: &RustMsg) {
        let Ok(json) = serde_json::to_string(msg) else {
            return;
        };
        let mut s = ascii_json(&json);
        s.push('\n');
        match self {
            Channel::Stdio => {
                let mut e = std::io::stderr();
                let _ = e.write_all(s.as_bytes());
                let _ = e.flush();
            }
            Channel::Socket(w) => {
                use tokio::io::AsyncWriteExt;
                let _ = w.write_all(s.as_bytes()).await;
            }
        }
    }
}

fn restore_terminal(enhanced: bool) {
    let mut out = std::io::stdout();
    if enhanced {
        let _ = execute!(out, PopKeyboardEnhancementFlags);
    }
    let _ = execute!(out, DisableBracketedPaste, DisableMouseCapture);
    let _ = disable_raw_mode();
    let _ = execute!(out, LeaveAlternateScreen, crossterm::cursor::Show);
}

/// Restores raw mode + main screen on every exit path (return, `?`, panic).
struct TermGuard {
    enhanced: bool,
}
impl TermGuard {
    fn new() -> Result<TermGuard, Box<dyn std::error::Error>> {
        // Shift+Enter is only distinguishable with the kitty keyboard
        // protocol on Unix; Windows consoles report modifiers natively.
        let enhanced = !cfg!(windows) && supports_keyboard_enhancement().unwrap_or(false);
        // ponytail: default panic hook + Drop guard, no extra cleanup paths
        let prev = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            restore_terminal(enhanced);
            prev(info);
        }));
        enable_raw_mode()?;
        let mut out = std::io::stdout();
        execute!(out, EnterAlternateScreen)?;
        // best effort: legacy consoles reject these and still work without
        let _ = execute!(out, EnableBracketedPaste, EnableMouseCapture);
        if enhanced {
            let _ = execute!(
                out,
                PushKeyboardEnhancementFlags(KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES)
            );
        }
        Ok(TermGuard { enhanced })
    }
}
impl Drop for TermGuard {
    fn drop(&mut self) {
        restore_terminal(self.enhanced);
    }
}

/// Outcome of reading one NDJSON line.
enum Read {
    Line,
    Oversized(usize),
    Eof,
}

/// Read one '\n'-terminated line into `buf`, keeping at most MAX_LINE bytes;
/// longer lines are consumed and reported as Oversized.
async fn read_capped<B>(r: &mut B, buf: &mut Vec<u8>) -> std::io::Result<Read>
where
    B: tokio::io::AsyncBufRead + Unpin,
{
    buf.clear();
    let mut dropped = 0usize;
    loop {
        let chunk = r.fill_buf().await?;
        if chunk.is_empty() {
            return Ok(match (buf.is_empty(), dropped) {
                (_, d) if d > 0 => Read::Oversized(d),
                (true, _) => Read::Eof,
                _ => Read::Line,
            });
        }
        let (take, done) = match chunk.iter().position(|&b| b == b'\n') {
            Some(i) => (i + 1, true),
            None => (chunk.len(), false),
        };
        if dropped == 0 && buf.len() + take <= MAX_LINE + 1 {
            buf.extend_from_slice(&chunk[..take]);
        } else {
            dropped += buf.len() + take;
            buf.clear();
        }
        r.consume(take);
        if done {
            return Ok(if dropped > 0 {
                Read::Oversized(dropped)
            } else {
                Read::Line
            });
        }
    }
}

/// NDJSON reader task (socket half or stdin). EOF/close reads as Quit so the
/// sidecar exits cleanly when the bridge goes away instead of hanging.
async fn read_task<B>(mut reader: B, tx: mpsc::UnboundedSender<PyMsg>)
where
    B: tokio::io::AsyncBufRead + Unpin,
{
    let mut buf = Vec::new();
    loop {
        match read_capped(&mut reader, &mut buf).await {
            Ok(Read::Line) => {
                let line = String::from_utf8_lossy(&buf);
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                match serde_json::from_str::<PyMsg>(line) {
                    Ok(m) => {
                        let quit = matches!(m, PyMsg::Quit);
                        let _ = tx.send(m);
                        if quit {
                            break;
                        }
                    }
                    Err(e) => {
                        // stderr would scribble over the alternate screen; show it in-chat
                        let head: String = line.chars().take(120).collect();
                        let _ = tx.send(PyMsg::System {
                            content: format!("Error: nim-tui bad message: {e}: {head}"),
                        });
                    }
                }
            }
            Ok(Read::Oversized(n)) => {
                let _ = tx.send(PyMsg::System {
                    content: format!("Warning: nim-tui dropped an oversized message ({n} bytes)"),
                });
            }
            _ => {
                let _ = tx.send(PyMsg::Quit);
                break;
            }
        }
    }
}

/// `nim-tui --demo`: sample content, no Python bridge needed.
fn demo(app: &mut app::App) {
    app.push("system", "nimcode · ~/projects/demo (--demo)");
    app.push("user", "show me some markdown");
    app.push(
        "assistant",
        "# Demo\n\nHello **bold**, `code`, and a list:\n\n- item one\n- item two\n\n| a | b |\n|---|---|\n| c | d |\n",
    );
    let body: String = (1..=30).map(|i| format!("line {i}\n")).collect();
    app.push("tool", &format!("Output ({} chars):\n{body}", body.len()));
    app.sidebar.model = "demo-model".into();
    app.sidebar.tokens_used = 41_000;
    app.completions = [
        "/help", "/plan", "/build", "/yolo", "/model", "/config", "/team", "/exit",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
}

#[tokio::main]
async fn main() -> R {
    let (tx, mut rx) = mpsc::unbounded_channel::<PyMsg>();
    let mut out = Channel::Stdio;
    if let Some(port) = std::env::var("NIM_TUI_PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
    {
        let stream = tokio::net::TcpStream::connect(("127.0.0.1", port)).await?;
        let _ = stream.set_nodelay(true);
        let (rh, wh) = stream.into_split();
        out = Channel::Socket(wh);
        tokio::spawn(read_task(tokio::io::BufReader::new(rh), tx.clone()));
    } else {
        tokio::spawn(read_task(
            tokio::io::BufReader::new(tokio::io::stdin()),
            tx.clone(),
        ));
    }
    drop(tx);

    let guard = TermGuard::new()?;
    let mut term = Terminal::new(CrosstermBackend::new(std::io::stdout()))?;
    let mut app = app::App::new(theme::Theme::from_env());
    app.shift_enter = cfg!(windows) || guard.enhanced;
    if std::env::args().any(|a| a == "--demo") {
        demo(&mut app);
    }
    // A plain thread feeding a channel: crossterm's Windows EventStream missed
    // wakeups inside select!, so keys were only seen on the next 1s idle tick.
    let (ev_tx, mut ev_rx) = mpsc::unbounded_channel::<Event>();
    std::thread::spawn(move || {
        while let Ok(e) = crossterm::event::read() {
            if ev_tx.send(e).is_err() {
                break;
            }
        }
    });
    let mut input_closed = false;
    let mut redraw = true;

    loop {
        while let Ok(m) = rx.try_recv() {
            redraw = true;
            if app.apply(m) {
                return Ok(());
            }
        }
        if redraw {
            term.draw(|f| ui::draw(f, &mut app))?;
            redraw = false;
        }
        let was_flashing = app.flashing();
        tokio::select! {
            ev = ev_rx.recv(), if !input_closed => {
                let first = match ev {
                    Some(e) => e,
                    None => {
                        input_closed = true;
                        continue;
                    }
                };
                // Drain everything already queued: keys queued behind an Enter
                // mean a paste (Windows consoles have no bracketed paste).
                let mut evs = vec![first];
                while evs.len() < 8192 {
                    match ev_rx.try_recv() {
                        Ok(e) => evs.push(e),
                        Err(mpsc::error::TryRecvError::Disconnected) => {
                            input_closed = true;
                            break;
                        }
                        Err(_) => break,
                    }
                }
                // Windows reports key release too; only presses/repeats count.
                evs.retain(|e| !matches!(e, Event::Key(k) if k.kind == KeyEventKind::Release));
                let n = evs.len();
                for (i, e) in evs.into_iter().enumerate() {
                    match e {
                        Event::Key(k) => {
                            redraw = true;
                            for m in app.on_key(k, i + 1 < n) {
                                out.emit(&m).await;
                            }
                        }
                        Event::Paste(s) => {
                            app.on_paste(&s);
                            redraw = true;
                        }
                        Event::Mouse(m) => redraw |= app.on_mouse(m),
                        Event::Resize(..) => redraw = true,
                        _ => {}
                    }
                }
            },
            Some(m) = rx.recv() => {
                redraw = true;
                if app.apply(m) {
                    return Ok(());
                }
            },
            _ = tokio::time::sleep(app.tick()) => {
                if app.sidebar.is_working {
                    app.spinner = app.spinner.wrapping_add(1);
                    redraw = true;
                }
                // repaint once when the completion flash ends
                redraw |= was_flashing && !app.flashing();
            }
        }
    }
}

/// Escape non-ASCII as \uXXXX (like Python's ensure_ascii) so the bridge
/// decodes our NDJSON whatever its socket file encoding is (cp1252 on Windows).
fn ascii_json(json: &str) -> String {
    let mut s = String::with_capacity(json.len());
    for c in json.chars() {
        if c.is_ascii() {
            s.push(c);
        } else {
            for u in c.encode_utf16(&mut [0u16; 2]) {
                s.push_str(&format!("\\u{u:04x}"));
            }
        }
    }
    s
}

/// Make incoming text terminal-safe: drop ANSI CSI sequences and control
/// chars (raw ESC would be written straight to the terminal), expand tabs.
fn clean_text(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        match c {
            '\n' => out.push('\n'),
            '\t' => out.push_str("    "),
            '\x1b' => {
                if it.peek() == Some(&'[') {
                    it.next();
                    // CSI: params until a final byte in '@'..='~'
                    for f in it.by_ref() {
                        if ('@'..='~').contains(&f) {
                            break;
                        }
                    }
                }
            }
            c if c.is_control() => {}
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn protocol_round_trip() {
        let py = vec![
            PyMsg::AddMessage {
                role: "user".into(),
                content: "hi".into(),
            },
            PyMsg::Update {
                sidebar: Sidebar::default(),
            },
            PyMsg::SetWorking {
                working: true,
                message: "x".into(),
            },
            PyMsg::Clear,
            PyMsg::PromptSelect {
                title: "t".into(),
                items: vec!["a".into()],
            },
            PyMsg::SetCompletions {
                items: vec!["/help".into()],
            },
            PyMsg::System {
                content: "s".into(),
            },
            PyMsg::Quit,
        ];
        for m in py {
            let s = serde_json::to_string(&m).unwrap();
            let back: PyMsg = serde_json::from_str(&s).unwrap();
            assert_eq!(serde_json::to_string(&back).unwrap(), s);
        }
        let rust = vec![
            RustMsg::Input { text: "hi".into() },
            RustMsg::SelectResult { index: Some(0) },
            RustMsg::SelectResult { index: None },
            RustMsg::ModeCycle {
                mode: "Build Mode".into(),
            },
            RustMsg::Interrupt,
            RustMsg::Eof,
        ];
        for m in rust {
            let s = serde_json::to_string(&m).unwrap();
            let back: RustMsg = serde_json::from_str(&s).unwrap();
            assert_eq!(serde_json::to_string(&back).unwrap(), s);
        }
        // bridge payload field names must match serde tags
        let v: serde_json::Value =
            serde_json::from_str(r#"{"type":"prompt_select","title":"t","items":[]}"#).unwrap();
        assert!(serde_json::from_str::<PyMsg>(&v.to_string()).is_ok());
        let v2: serde_json::Value =
            serde_json::from_str(r#"{"type":"set_completions","items":["/help"]}"#).unwrap();
        assert!(serde_json::from_str::<PyMsg>(&v2.to_string()).is_ok());
    }
    #[test]
    fn protocol_matches_shared_samples() {
        // Shared with tests/test_bridge_protocol.py: drift on either side fails.
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../tests/protocol_samples.json"
        );
        let raw = std::fs::read_to_string(path).expect("read protocol_samples.json");
        let samples: serde_json::Value = serde_json::from_str(&raw).unwrap();
        for s in samples["py_to_rust"].as_array().unwrap() {
            let m: PyMsg = serde_json::from_value(s.clone())
                .unwrap_or_else(|e| panic!("PyMsg rejects {s}: {e}"));
            assert_eq!(serde_json::to_value(&m).unwrap()["type"], s["type"]);
        }
        let rust_samples = samples["rust_to_py"].as_array().unwrap();
        for s in rust_samples {
            let m: RustMsg = serde_json::from_value(s.clone())
                .unwrap_or_else(|e| panic!("RustMsg rejects {s}: {e}"));
            assert_eq!(&serde_json::to_value(&m).unwrap(), s);
        }
        // every message Rust can emit has a sample with the same keys
        let emitted = vec![
            RustMsg::Input { text: "x".into() },
            RustMsg::SelectResult { index: Some(0) },
            RustMsg::ModeCycle {
                mode: "Build Mode".into(),
            },
            RustMsg::Interrupt,
            RustMsg::Eof,
        ];
        for m in emitted {
            let v = serde_json::to_value(&m).unwrap();
            let keys = |o: &serde_json::Value| {
                let mut k: Vec<String> = o.as_object().unwrap().keys().cloned().collect();
                k.sort();
                k
            };
            assert!(
                rust_samples
                    .iter()
                    .any(|s| s["type"] == v["type"] && keys(s) == keys(&v)),
                "no sample for {v}"
            );
        }
    }
    #[test]
    fn ascii_emit_and_clean_text() {
        let j = serde_json::to_string(&RustMsg::Input {
            text: "é😀".into()
        })
        .unwrap();
        let a = ascii_json(&j);
        assert!(a.is_ascii(), "{a}");
        let back: RustMsg = serde_json::from_str(&a).unwrap();
        assert_eq!(serde_json::to_string(&back).unwrap(), j);
        assert_eq!(clean_text("\x1b[31mred\x1b[0m\tx\r\ny\x07"), "red    x\ny");
    }
    #[tokio::test]
    async fn oversized_line_is_dropped() {
        let big = format!(
            "{{\"type\":\"system\",\"content\":\"{}\"}}\n",
            "x".repeat(MAX_LINE + 10)
        );
        let data = format!("{big}{{\"type\":\"system\",\"content\":\"ok\"}}\n");
        let (tx, mut rx) = mpsc::unbounded_channel();
        read_task(tokio::io::BufReader::new(data.as_bytes()), tx).await;
        assert!(
            matches!(rx.recv().await, Some(PyMsg::System { content }) if content.contains("oversized"))
        );
        assert!(matches!(rx.recv().await, Some(PyMsg::System { content }) if content == "ok"));
        assert!(matches!(rx.recv().await, Some(PyMsg::Quit)));
    }
}
