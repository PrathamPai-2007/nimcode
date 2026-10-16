use crossterm::event::{Event, EventStream, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::{execute, terminal::*};
use futures::StreamExt;
use pulldown_cmark::{Event as MdEv, Options, Parser, Tag, TagEnd};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, List, ListItem, Paragraph},
    Terminal,
};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::io::Write;
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

fn role_color(role: &str) -> Color {
    match role {
        "user" => Color::Cyan,
        "assistant" => Color::Green,
        "tool" => Color::Yellow,
        _ => Color::DarkGray,
    }
}

// Protocol channel: NDJSON over stdout (direct `--demo`/pipe use) or over a TCP
// loopback socket (bridge mode, NIM_TUI_PORT) so stdio stays a real terminal.
// All emits happen in the main task; no lock needed.
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
                print!("{s}");
                let _ = std::io::stdout().flush();
            }
            Channel::Socket(w) => {
                use tokio::io::AsyncWriteExt;
                let _ = w.write_all(s.as_bytes()).await;
            }
        }
    }
}

/// Restores raw mode + main screen on every exit path (return, `?`, panic).
struct TermGuard;
impl TermGuard {
    fn new() -> Result<TermGuard, Box<dyn std::error::Error>> {
        // ponytail: default panic hook + Drop guard, no extra cleanup paths
        let prev = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            let _ = disable_raw_mode();
            let _ = execute!(std::io::stdout(), LeaveAlternateScreen, crossterm::cursor::Show);
            prev(info);
        }));
        enable_raw_mode()?;
        execute!(std::io::stdout(), EnterAlternateScreen)?;
        Ok(TermGuard)
    }
}
impl Drop for TermGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(std::io::stdout(), LeaveAlternateScreen, crossterm::cursor::Show);
    }
}

// ---------- markdown ----------

#[derive(Default, Clone)]
struct Sty {
    bold: bool,
    italic: bool,
    code: bool,
    heading: bool,
    quote: bool,
    link: bool,
    strike: bool,
}

impl Sty {
    fn style(&self) -> Style {
        let mut s = Style::default().fg(if self.code {
            Color::Yellow
        } else if self.heading {
            Color::Cyan
        } else if self.quote {
            Color::Gray
        } else if self.link {
            Color::Blue
        } else {
            Color::Reset
        });
        if self.bold || self.heading {
            s = s.add_modifier(Modifier::BOLD);
        }
        if self.italic || self.quote {
            s = s.add_modifier(Modifier::ITALIC);
        }
        if self.link {
            s = s.add_modifier(Modifier::UNDERLINED);
        }
        if self.strike {
            s = s.add_modifier(Modifier::CROSSED_OUT);
        }
        if self.code {
            s = s.bg(Color::Rgb(30, 30, 30));
        }
        s
    }
}

/// Parse markdown into styled lines (unwrapped).
fn markdown_lines(md: &str) -> Vec<Line<'static>> {
    let mut opts = Options::empty();
    opts.insert(Options::ENABLE_STRIKETHROUGH);
    opts.insert(Options::ENABLE_TABLES);
    opts.insert(Options::ENABLE_TASKLISTS);
    let parser = Parser::new_ext(md, opts);
    let mut out: Vec<Line<'static>> = vec![];
    let mut cur: Vec<Span<'static>> = vec![];
    let mut sty = Sty::default();
    let mut list_stack: Vec<Option<u64>> = vec![]; // None = bullet, Some(n) = ordered
    let mut line_prefix = String::new();

    let flush = |cur: &mut Vec<Span<'static>>, out: &mut Vec<Line<'static>>| {
        if cur.is_empty() {
            out.push(Line::from(""));
        } else {
            out.push(Line::from(std::mem::take(cur)));
        }
    };

    for ev in parser {
        match ev {
            MdEv::Start(tag) => match tag {
                Tag::Heading { .. } => {
                    if !cur.is_empty() {
                        flush(&mut cur, &mut out);
                    }
                    sty.heading = true;
                    sty.bold = true;
                    line_prefix = "## ".into();
                }
                Tag::Strong => sty.bold = true,
                Tag::Emphasis => sty.italic = true,
                Tag::Strikethrough => sty.strike = true,
                Tag::Link { .. } => sty.link = true,
                Tag::CodeBlock(_) => {
                    if !cur.is_empty() {
                        flush(&mut cur, &mut out);
                    }
                    sty.code = true;
                }
                Tag::BlockQuote(_) => sty.quote = true,
                Tag::List(n) => list_stack.push(n),
                Tag::Table(_) | Tag::TableHead | Tag::TableRow => {
                    if !cur.is_empty() {
                        flush(&mut cur, &mut out);
                    }
                }
                Tag::TableCell => {
                    if !cur.is_empty() {
                        cur.push(Span::raw(" │ "));
                    }
                }
                Tag::Item => {
                    if !cur.is_empty() {
                        flush(&mut cur, &mut out);
                    }
                    let depth = list_stack.len().saturating_sub(1);
                    let marker = match list_stack.last() {
                        Some(Some(n)) => format!("{n}. "),
                        _ => "• ".to_string(),
                    };
                    if let Some(Some(n)) = list_stack.last_mut() {
                        *n += 1;
                    }
                    line_prefix = format!("{}{}", "  ".repeat(depth), marker);
                }
                Tag::Paragraph => {
                    if !cur.is_empty() {
                        flush(&mut cur, &mut out);
                    }
                }
                _ => {}
            },
            MdEv::End(end) => match end {
                TagEnd::Heading(_) => {
                    flush(&mut cur, &mut out);
                    sty.heading = false;
                    sty.bold = false;
                }
                TagEnd::Strong => sty.bold = false,
                TagEnd::Emphasis => sty.italic = false,
                TagEnd::Strikethrough => sty.strike = false,
                TagEnd::Link => sty.link = false,
                TagEnd::CodeBlock => {
                    flush(&mut cur, &mut out);
                    sty.code = false;
                }
                TagEnd::BlockQuote(_) => sty.quote = false,
                TagEnd::List(_) => {
                    list_stack.pop();
                }
                TagEnd::TableRow
                | TagEnd::TableHead
                | TagEnd::Table => flush(&mut cur, &mut out),
                TagEnd::Item | TagEnd::Paragraph => flush(&mut cur, &mut out),
                _ => {}
            },
            MdEv::Text(t) => {
                let s = t.into_string();
                for (i, part) in s.split('\n').enumerate() {
                    if i > 0 {
                        flush(&mut cur, &mut out);
                    }
                    if !line_prefix.is_empty() {
                        let pre = std::mem::take(&mut line_prefix);
                        let q = if sty.quote { "▌ " } else { "" };
                        cur.push(Span::styled(
                            format!("{q}{pre}"),
                            Style::default()
                                .fg(Color::Cyan)
                                .add_modifier(Modifier::BOLD),
                        ));
                    } else if sty.quote
                        && cur.is_empty()
                        && !part.is_empty()
                    {
                        cur.push(Span::styled(
                            "▌ ",
                            Style::default().fg(Color::Gray),
                        ));
                    }
                    if !part.is_empty() {
                        cur.push(Span::styled(part.to_string(), sty.style()));
                    }
                }
            }
            MdEv::Code(c) => {
                let mut csty = sty.clone();
                csty.code = true;
                cur.push(Span::styled(
                    format!(" `{}` ", c.into_string()),
                    csty.style(),
                ));
            }
            MdEv::SoftBreak | MdEv::HardBreak => flush(&mut cur, &mut out),
            MdEv::Rule => out.push(Line::from(Span::styled(
                "─".repeat(40),
                Style::default().fg(Color::DarkGray),
            ))),
            MdEv::TaskListMarker(done) => {
                cur.push(Span::raw(if done { "✓ " } else { "○ " }));
            }
            _ => {}
        }
    }
    if !cur.is_empty() {
        out.push(Line::from(cur));
    }
    if out.is_empty() {
        out.push(Line::from(""));
    }
    out
}

/// Greedy char-aware wrap that keeps span styles.
// ponytail: O(chars); virtualize if history > 20k lines matters
fn wrap_lines(src: Vec<Line<'static>>, width: usize) -> Vec<Line<'static>> {
    let w = width.max(10);
    let mut out = vec![];
    for line in src {
        let mut words: Vec<Vec<(char, Style)>> = vec![];
        let mut curw: Vec<(char, Style)> = vec![];
        for sp in &line.spans {
            for ch in sp.content.chars() {
                if ch == ' ' {
                    if !curw.is_empty() {
                        words.push(std::mem::take(&mut curw));
                    }
                    words.push(vec![(' ', sp.style)]);
                } else {
                    curw.push((ch, sp.style));
                }
            }
        }
        if !curw.is_empty() {
            words.push(curw);
        }
        if words.is_empty() {
            out.push(Line::from(""));
            continue;
        }
        let mut cur: Vec<Span<'static>> = vec![];
        let mut len = 0usize;
        let mut run = String::new();
        let mut run_style = words[0][0].1;
        for word in words {
            let ww: usize = word.iter().map(|(ch, _)| ch.width().unwrap_or(0)).sum();
            if ww > w {
                // oversized word (URL/hash): chunk into width-fitting pieces
                push_run(&mut run, run_style, &mut cur);
                if !cur.is_empty() {
                    out.push(Line::from(std::mem::take(&mut cur)));
                    len = 0;
                }
                for (ch, st) in word {
                    let cw = ch.width().unwrap_or(0);
                    if st != run_style {
                        push_run(&mut run, run_style, &mut cur);
                        run_style = st;
                    }
                    if cw > 0 && len + cw > w && len > 0 {
                        push_run(&mut run, run_style, &mut cur);
                        out.push(Line::from(std::mem::take(&mut cur)));
                        len = 0;
                    }
                    run.push(ch);
                    len += cw;
                }
                continue;
            }
            if len + ww > w && len > 0 {
                push_run(&mut run, run_style, &mut cur);
                out.push(Line::from(std::mem::take(&mut cur)));
                len = 0;
            }
            for (ch, st) in word {
                if st != run_style {
                    push_run(&mut run, run_style, &mut cur);
                    run_style = st;
                }
                run.push(ch);
                len += ch.width().unwrap_or(0);
            }
        }
        push_run(&mut run, run_style, &mut cur);
        out.push(Line::from(cur));
    }
    out
}

fn push_run(run: &mut String, style: Style, cur: &mut Vec<Span<'static>>) {
    if !run.is_empty() {
        cur.push(Span::styled(std::mem::take(run), style));
    }
}

fn render_md(content: &str, width: usize) -> Vec<Line<'static>> {
    wrap_lines(markdown_lines(content), width.max(10))
}

fn trunc(s: &str, n: usize) -> String {
    if s.width() <= n {
        return s.into();
    }
    let mut out = String::new();
    let mut w = 0usize;
    for ch in s.chars() {
        let cw = ch.width().unwrap_or(0);
        if w + cw > n.saturating_sub(1) {
            break;
        }
        out.push(ch);
        w += cw;
    }
    out + "…"
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

fn centered(area: Rect, w: u16, h: u16) -> Rect {
    Rect {
        x: area.x + area.width.saturating_sub(w) / 2,
        y: area.y + area.height.saturating_sub(h) / 2,
        width: w.min(area.width),
        height: h.min(area.height),
    }
}

/// NDJSON reader task (socket half or stdin). EOF/close reads as Quit so the
/// sidecar exits cleanly when the bridge goes away instead of hanging.
async fn read_task<R>(reader: R, tx: mpsc::UnboundedSender<PyMsg>)
where
    R: tokio::io::AsyncBufRead + Unpin,
{
    let mut lines = tokio::io::AsyncBufReadExt::lines(reader);
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                if line.trim().is_empty() {
                    continue;
                }
                match serde_json::from_str::<PyMsg>(&line) {
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
                            content: format!("nim-tui: bad message: {e}: {head}"),
                        });
                    }
                }
            }
            _ => {
                let _ = tx.send(PyMsg::Quit);
                break;
            }
        }
    }
}

// Slash-command autocomplete helpers (pure; unit-tested below).

/// The word being completed, if the input is a bare `/command` fragment
/// (starts with '/', no whitespace yet).
fn completion_word(input: &str) -> Option<&str> {
    if !input.starts_with('/') {
        return None;
    }
    let word = &input[1..];
    if word.contains(char::is_whitespace) {
        return None;
    }
    Some(word)
}

/// Catalog indices whose entry matches the word (prefix, case-insensitive).
/// Catalog entries carry their leading '/' (e.g. "/help").
fn filter_completions(items: &[String], word: &str) -> Vec<usize> {
    let w = word.to_lowercase();
    items
        .iter()
        .enumerate()
        .filter(|(_, it)| {
            it.strip_prefix('/')
                .unwrap_or(it.as_str())
                .to_lowercase()
                .starts_with(&w)
        })
        .map(|(i, _)| i)
        .collect()
}

/// Indices to show for the current input (empty when no popup should open).
fn popup_matches(completions: &[String], input: &str) -> Vec<usize> {
    match completion_word(input) {
        Some(w) => filter_completions(completions, w),
        None => vec![],
    }
}

/// Accept a completion: replace the command word with the full entry,
// preserving any trailing args the user already typed.
fn accept_completion(input: &str, entry: &str) -> String {
    match input.find(char::is_whitespace) {
        Some(i) => format!("{entry}{}", &input[i..]),
        None => entry.to_string(),
    }
}

#[tokio::main]
async fn main() -> R {
    // Bridge mode: Python listens on 127.0.0.1 and passes NIM_TUI_PORT, so the
    // sidecar's stdio stays attached to the real terminal for crossterm.
    // Without it (direct run, `--demo`, pipes) NDJSON flows over stdio.
    let (tx, mut rx) = mpsc::unbounded_channel::<PyMsg>();
    let mut out = Channel::Stdio;
    if let Some(port) = std::env::var("NIM_TUI_PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
    {
        let stream = tokio::net::TcpStream::connect(("127.0.0.1", port)).await?;
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

    let _guard = TermGuard::new()?;
    let backend = CrosstermBackend::new(std::io::stdout());
    let mut term = Terminal::new(backend)?;

    let mut messages: VecDeque<(String, String)> = VecDeque::from([(
        "system".into(),
        "Welcome to Nimcode (Rust TUI). Type a task to begin.".into(),
    )]);
    let demo = std::env::args().any(|a| a == "--demo");
    // standalone check: `nim-tui --demo` runs interactively with sample content,
    // no Python bridge needed. Piped stdin still quits on EOF by design.
    if demo {
        messages.push_back((
            "assistant".into(),
            "# Demo\n\nHello **bold**, `code`, and a list:\n\n- item one\n- item two\n\n| a | b |\n|---|---|\n| c | d |\n".into(),
        ));
    }
    let mut sidebar = Sidebar::default();
    let mut input = String::new();
    // slash-command autocomplete (catalog pushed by Python via SetCompletions;
    // sample catalog in --demo so the popup is previewable standalone)
    let mut completions: Vec<String> = if demo {
        vec![
            "/help".into(),
            "/plan".into(),
            "/build".into(),
            "/yolo".into(),
            "/model".into(),
            "/config".into(),
            "/team".into(),
            "/exit".into(),
        ]
    } else {
        vec![]
    };
    let mut comp_idx: usize = 0;
    let mut comp_dismissed_for: Option<String> = None;
    let mut scroll: usize = 0;
    let mut stick_bottom = true;
    let mut cached: Vec<Line> = vec![];
    let mut dirty = true;
    let mut last_w: usize = 0;
    let mut last_spin = tokio::time::Instant::now();
    let mut selector: Option<(String, Vec<String>, usize, usize)> = None;
    let modes = ["Plan Mode", "Build Mode", "YOLO Mode"];
    let mut spinner = 0usize;
    let mut reader = EventStream::new();
    let mut input_closed = false;
    let mut pending: Option<PyMsg> = None;
    let mut redraw = true;

    loop {
        // drain python messages (`pending` = one already received by select!)
        while let Some(m) = pending.take().or_else(|| rx.try_recv().ok()) {
            redraw = true;
            match m {
                PyMsg::AddMessage { role, content } => {
                    messages.push_back((role, clean_text(&content)));
                    stick_bottom = true;
                    dirty = true;
                }
                PyMsg::System { content } => {
                    messages.push_back(("system".into(), clean_text(&content)));
                    stick_bottom = true;
                    dirty = true;
                }
                // set_working is authoritative for the spinner; the bridge's
                // sidebar dict always carries is_working=false.
                PyMsg::Update { sidebar: mut s } => {
                    s.is_working = sidebar.is_working;
                    s.working_message = std::mem::take(&mut sidebar.working_message);
                    sidebar = s;
                }
                PyMsg::SetWorking { working, message } => {
                    sidebar.is_working = working;
                    sidebar.working_message = message;
                }
                PyMsg::Clear => {
                    messages.clear();
                    scroll = 0;
                    stick_bottom = true;
                    dirty = true;
                }
                PyMsg::PromptSelect { title, items } => {
                    selector = Some((title, items, 0, 0))
                }
                PyMsg::SetCompletions { items } => {
                    completions = items;
                    comp_idx = 0;
                    comp_dismissed_for = None;
                }
                PyMsg::Quit => {
                    return Ok(());
                }
            }
            if messages.len() > 5000 {
                messages.pop_front();
            }
        }

        // Block until a key, a Python message, or the spinner tick — no 5ms
        // polling spin. Ignore key-release events: on Windows crossterm
        // reports press AND release, so handling both would double every
        // keystroke (and double-fire Enter/Ctrl+C). Repeats still count.
        let tick = if sidebar.is_working { 100 } else { 1000 };
        let mut key = None;
        tokio::select! {
            ev = reader.next(), if !input_closed => match ev {
                Some(Ok(Event::Key(k))) if k.kind != KeyEventKind::Release => key = Some(k),
                Some(Ok(_)) => redraw = true, // resize etc.
                Some(Err(_)) => {}
                None => input_closed = true,
            },
            Some(m) = rx.recv() => pending = Some(m),
            _ = tokio::time::sleep(std::time::Duration::from_millis(tick)) => {}
        }
        if pending.is_some() {
            continue;
        }
        if let Some(k) = key {
            redraw = true;
            if let Some((_, items, idx, _)) = selector.as_mut() {
                let empty = items.is_empty();
                let nav = matches!(
                    k.code,
                    KeyCode::Up
                        | KeyCode::Down
                        | KeyCode::PageUp
                        | KeyCode::PageDown
                        | KeyCode::Home
                        | KeyCode::End
                );
                match k.code {
                    KeyCode::Up => *idx = idx.saturating_sub(1),
                    KeyCode::Down => *idx = (*idx + 1).min(items.len().saturating_sub(1)),
                    KeyCode::PageUp => *idx = idx.saturating_sub(10),
                    KeyCode::PageDown => {
                        *idx = (*idx + 10).min(items.len().saturating_sub(1))
                    }
                    KeyCode::Home => *idx = 0,
                    KeyCode::End => *idx = items.len().saturating_sub(1),
                    KeyCode::Enter if !empty => {
                        let i = *idx;
                        selector = None;
                        out.emit(&RustMsg::SelectResult { index: Some(i) }).await;
                    }
                    KeyCode::Esc => {
                        selector = None;
                        out.emit(&RustMsg::SelectResult { index: None }).await;
                    }
                    KeyCode::Enter => {
                        // empty list: nothing to pick
                        selector = None;
                        out.emit(&RustMsg::SelectResult { index: None }).await;
                    }
                    KeyCode::Char('c')
                        if k.modifiers.contains(KeyModifiers::CONTROL) =>
                    {
                        selector = None;
                        out.emit(&RustMsg::SelectResult { index: None }).await;
                    }
                    _ => {}
                }
                // Keep the highlight inside the visible window (no-op once
                // the selector was dismissed above).
                if nav {
                    if let Some((_, items, idx, top)) = selector.as_mut() {
                        let n = items.len();
                        *top = selector_follow(*top, *idx, selector_visible(n), n);
                    }
                }
            } else {
                // slash-command autocomplete popup (client-side filter)
                let matches = popup_matches(&completions, &input);
                let dismissed =
                    comp_dismissed_for.as_deref() == Some(input.as_str());
                let popup_open = !matches.is_empty() && !dismissed;
                if popup_open && comp_idx >= matches.len() {
                    comp_idx = 0;
                }
                let sel = matches.get(comp_idx).copied();
                let mut cycle = false;
                match k.code {
                    KeyCode::Tab if popup_open => {
                        if let Some(ci) = sel {
                            input = accept_completion(&input, &completions[ci]);
                            comp_dismissed_for = None;
                            let nm = popup_matches(&completions, &input);
                            comp_idx = if nm.is_empty() {
                                0
                            } else {
                                comp_idx.min(nm.len() - 1)
                            };
                        }
                    }
                    KeyCode::Up if popup_open => {
                        comp_idx = comp_idx.saturating_sub(1)
                    }
                    KeyCode::Down if popup_open => {
                        comp_idx = (comp_idx + 1).min(matches.len().saturating_sub(1))
                    }
                    KeyCode::Esc if popup_open => {
                        comp_dismissed_for = Some(input.clone())
                    }
                    KeyCode::Enter => {
                        out.emit(&RustMsg::Input {
                            text: std::mem::take(&mut input),
                        })
                        .await;
                        comp_idx = 0;
                        comp_dismissed_for = None;
                        stick_bottom = true;
                    }
                    KeyCode::Backspace => {
                        input.pop();
                        comp_idx = 0;
                        comp_dismissed_for = None;
                    }
                    KeyCode::Char('c') if k.modifiers.contains(KeyModifiers::CONTROL) => {
                        if input.is_empty() {
                            out.emit(&RustMsg::Interrupt).await;
                        } else {
                            input.clear();
                        }
                        comp_idx = 0;
                        comp_dismissed_for = None;
                    }
                    KeyCode::Char('d') if k.modifiers.contains(KeyModifiers::CONTROL) => {
                        out.emit(&RustMsg::Eof).await
                    }
                    KeyCode::BackTab => cycle = true,
                    KeyCode::Char('t') if k.modifiers.contains(KeyModifiers::CONTROL) => {
                        cycle = true
                    }
                    KeyCode::Esc => stick_bottom = false,
                    KeyCode::End => stick_bottom = true,
                    KeyCode::Home => {
                        stick_bottom = false;
                        scroll = 0;
                    }
                    // scrolling up must unstick, or the next draw snaps back down
                    KeyCode::Up => {
                        stick_bottom = false;
                        scroll = scroll.saturating_sub(1)
                    }
                    KeyCode::PageUp => {
                        stick_bottom = false;
                        scroll = scroll.saturating_sub(10)
                    }
                    KeyCode::Down => scroll = scroll.saturating_add(1),
                    KeyCode::PageDown => scroll = scroll.saturating_add(10),
                    // Ctrl+letter is a shortcut, not text (Ctrl+Alt = AltGr on Windows)
                    KeyCode::Char(_)
                        if k.modifiers.contains(KeyModifiers::CONTROL)
                            && !k.modifiers.contains(KeyModifiers::ALT) => {}
                    KeyCode::Char(c) => {
                        input.push(c);
                        comp_dismissed_for = None;
                    }
                    _ => {}
                }
                if cycle {
                    // cycle from the mode Python last reported, not a local
                    // counter that starts at Build regardless of config.mode
                    let cur = modes.iter().position(|m| *m == sidebar.mode).unwrap_or(1);
                    let next = modes[(cur + 1) % modes.len()];
                    sidebar.mode = next.into();
                    out.emit(&RustMsg::ModeCycle { mode: next.into() }).await;
                }
            }
        }

        let w_for_md: usize = term.size()?.width.saturating_sub(41) as usize;
        let spin_due = sidebar.is_working
            && last_spin.elapsed() >= std::time::Duration::from_millis(100);
        if spin_due {
            last_spin = tokio::time::Instant::now();
            spinner = spinner.wrapping_add(1);
        }
        let rebuilt = dirty || last_w != w_for_md;
        if rebuilt {
            // ponytail: incremental render cache; full re-parse only on change/resize
            last_w = w_for_md;
            dirty = false;
            let mut lines: Vec<Line> = vec![];
            for (role, content) in messages.iter() {
            lines.push(Line::from(vec![Span::styled(
                format!("{}:", role),
                Style::default()
                    .fg(role_color(role))
                    .add_modifier(Modifier::BOLD),
            )]));
            if role == "assistant" || role == "system" {
                lines.extend(render_md(content, w_for_md.max(20)));
            } else {
                for chunk in textwrap::wrap(content, w_for_md.max(20)).iter() {
                    lines.push(Line::from(Span::styled(
                        chunk.to_string(),
                        Style::default().fg(Color::White),
                    )));
                }
            }
            lines.push(Line::from(""));
            }
            cached = lines;
        }
        if !(rebuilt || redraw || spin_due) {
            continue;
        }
        redraw = false;

        term.draw(|f| {
            let size = f.area();
            let root = Layout::default()
                .direction(Direction::Vertical)
                .constraints([Constraint::Min(3), Constraint::Length(3)])
                .split(size);
            let body = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Min(20), Constraint::Length(37)])
                .split(root[0]);

            let total = cached.len();
            let h = body[0].height.saturating_sub(2) as usize;
            // clamp so Down/PgDn can't run past the end into a blank pane;
            // reaching the bottom re-sticks it
            let max_scroll = total.saturating_sub(h);
            if stick_bottom || scroll >= max_scroll {
                scroll = max_scroll;
                stick_bottom = true;
            }
            let start = scroll.min(cached.len());
            let end = (start + h).min(cached.len());
            let list: Vec<ListItem> =
                cached[start..end].iter().cloned().map(ListItem::new).collect();
            f.render_widget(
                List::new(list)
                    .block(Block::default().borders(Borders::ALL).title(" nimcode ")),
                body[0],
            );

            let pct = if sidebar.tokens_budget > 0 {
                sidebar.tokens_used as f64 / sidebar.tokens_budget as f64 * 100.0
            } else {
                0.0
            };
            let frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
            let mut s: Vec<Line> = vec![
                Line::from(vec![Span::styled(
                    format!(" {} ", sidebar.mode),
                    Style::default()
                        .fg(Color::Green)
                        .add_modifier(Modifier::BOLD),
                )]),
                Line::from(format!("model: {}", sidebar.model)),
                Line::from(format!(
                    "tokens: {} / {} ({:.0}%)",
                    sidebar.tokens_used, sidebar.tokens_budget, pct
                )),
                Line::from(format!("cwd: {}", sidebar.cwd)),
            ];
            if sidebar.is_working {
                s.push(Line::from(vec![Span::styled(
                    format!("{} {}", frames[spinner % frames.len()], sidebar.working_message),
                    Style::default().fg(Color::Cyan),
                )]));
            }
            if !sidebar.todos.is_empty() {
                s.push(Line::from("TODOS:"));
                for t in sidebar.todos.iter().take(8) {
                    s.push(Line::from(format!(
                        " {} {}",
                        if t.done { "✓" } else { "○" },
                        trunc(&t.text, 30)
                    )));
                }
            }
            if !sidebar.subagents.is_empty() {
                s.push(Line::from("SUBAGENTS:"));
                for a in sidebar.subagents.iter().take(5) {
                    s.push(Line::from(format!(
                        " ● {}: {}",
                        trunc(&a.role, 18),
                        a.status
                    )));
                }
            }
            if !sidebar.procs.is_empty() {
                s.push(Line::from("PROCS:"));
                for p in sidebar.procs.iter().take(5) {
                    s.push(Line::from(format!(
                        " ⚙ {} ({})",
                        trunc(&p.command, 26),
                        p.pid
                    )));
                }
            }
            s.push(Line::from(""));
            s.push(Line::from(Span::styled(
                "S-Tab mode | Esc scroll | ?: help",
                Style::default().fg(Color::DarkGray),
            )));
            f.render_widget(
                Paragraph::new(s)
                    .block(Block::default().borders(Borders::ALL).title(" status ")),
                body[1],
            );

            if let Some((title, items, idx, top)) = selector.as_ref() {
                let n = items.len();
                let visible = selector_visible(n);
                let top = (*top).min(n.saturating_sub(1));
                let end = (top + visible).min(n);
                let mut sl: Vec<Line> = vec![
                    Line::from(vec![Span::styled(
                        title.clone(),
                        Style::default()
                            .fg(Color::Cyan)
                            .add_modifier(Modifier::BOLD),
                    )]),
                    Line::from(""),
                ];
                for (j, it) in items[top..end].iter().enumerate() {
                    let i = top + j;
                    let st = if i == *idx {
                        Style::default().fg(Color::Black).bg(Color::Cyan)
                    } else {
                        Style::default()
                    };
                    sl.push(Line::from(vec![Span::styled(
                        format!("{} {}", if i == *idx { "►" } else { " " }, trunc(it, 60)),
                        st,
                    )]));
                }
                sl.push(Line::from(""));
                let mut hint =
                    String::from("↑/↓ navigate • PgUp/PgDn • Enter select • Esc cancel");
                if top > 0 {
                    hint = format!("▲{top} more • {hint}");
                }
                if end < n {
                    hint = format!("{hint} • ▼{} more", n - end);
                }
                sl.push(Line::from(hint));
                let area = centered(f.area(), 60, (items.len().min(18) + 6) as u16);
                f.render_widget(Clear, area);
                f.render_widget(
                    Paragraph::new(sl).block(Block::default().borders(Borders::ALL)),
                    area,
                );
            }

            let prompt = if selector.is_some() { "SELECT" } else { ">" };
            // show the tail of long input so the caret stays on screen
            let avail = (root[1].width as usize).saturating_sub(prompt.len() + 4);
            let mut shown = input.as_str();
            let mut tw = 0usize;
            for (i, ch) in input.char_indices().rev() {
                tw += ch.width().unwrap_or(0);
                if tw > avail {
                    shown = &input[i + ch.len_utf8()..];
                    break;
                }
            }
            f.render_widget(
                Paragraph::new(format!("{prompt} {shown}▌"))
                    .block(Block::default().borders(Borders::ALL)),
                root[1],
            );

            // slash-command autocomplete popup, anchored above the input bar
            if selector.is_none() {
                let pm = popup_matches(&completions, &input);
                let dismissed =
                    comp_dismissed_for.as_deref() == Some(input.as_str());
                if !pm.is_empty() && !dismissed {
                    let ci = comp_idx.min(pm.len() - 1);
                    // scroll the 8-row window so the highlight stays visible
                    let rows = pm.iter().skip(ci.saturating_sub(7)).take(8).cloned().collect::<Vec<_>>();
                    let sel_ci = pm[ci];
                    let mut cl: Vec<Line> = vec![];
                    for mi in rows {
                        let active = mi == sel_ci;
                        let st = if active {
                            Style::default().fg(Color::Black).bg(Color::Cyan)
                        } else {
                            Style::default()
                        };
                        cl.push(Line::from(vec![Span::styled(
                            format!(
                                "{} {}",
                                if active { "►" } else { " " },
                                trunc(&completions[mi], 44)
                            ),
                            st,
                        )]));
                    }
                    cl.push(Line::from(Span::styled(
                        "Tab accept • ↑/↓ navigate • Enter send • Esc dismiss",
                        Style::default().fg(Color::DarkGray),
                    )));
                    let h = (cl.len() as u16 + 2).min(12);
                    let w = 50.min(size.width.saturating_sub(2)).max(24);
                    // clamp to the rows above the input bar so the popup
                    // never overflows past it on short terminals
                    let h = h.min(root[1].y.max(1));
                    // intersect with the frame: on terminals < 25 cols the
                    // min width 24 would index outside the buffer and panic
                    let area = Rect {
                        x: 1,
                        y: root[1].y.saturating_sub(h),
                        width: w,
                        height: h.max(1),
                    }
                    .intersection(size);
                    f.render_widget(Clear, area);
                    f.render_widget(
                        Paragraph::new(cl)
                            .block(Block::default().borders(Borders::ALL).title(" commands ")),
                        area,
                    );
                }
            }
        })?;
    }
}

/// Rows of the selector modal available for items (modal height minus chrome).
fn selector_visible(item_count: usize) -> usize {
    let h = (item_count + 6).min(24);
    h.saturating_sub(6).max(1)
}

/// Keep `idx` visible: move the window `top` just enough, clamped to range.
fn selector_follow(top: usize, idx: usize, visible: usize, total: usize) -> usize {
    if total <= visible {
        return 0;
    }
    let max_top = total - visible;
    let mut t = top.min(max_top);
    if idx < t {
        t = idx;
    } else if idx >= t + visible {
        t = idx + 1 - visible;
    }
    t.min(max_top)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn plain(lines: &[Line]) -> String {
        lines
            .iter()
            .map(|l| l.spans.iter().map(|s| s.content.to_string()).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n")
    }
    #[test]
    fn heading_bold_code_list() {
        let md = "# Hi\n\nHello **bold** and *ital* `code`.\n\n- a\n- b\n\n```\nfn x() {}\n```\n";
        let lines = render_md(md, 40);
        let t = plain(&lines);
        assert!(t.contains("Hi"), "{t}");
        assert!(t.contains("bold") && t.contains("code"), "{t}");
        assert!(t.contains("• a"), "{t}");
        assert!(t.contains("fn x()"), "{t}");
        // heading style is bold cyan
        let h = lines.iter().find(|l| l.spans.iter().any(|s| s.content.contains("Hi"))).unwrap();
        assert!(h.spans.iter().any(|s| s.style.add_modifier.contains(Modifier::BOLD)));
    }
    #[test]
    fn wrap_keeps_words() {
        let lines = render_md("word ".repeat(30).trim(), 20);
        assert!(lines.len() > 3);
        assert!(lines.iter().all(|l| l.width() <= 20), "{}", plain(&lines));
    }
    #[test]
    fn empty_safe() {
        assert!(!render_md("", 40).is_empty());
    }
    #[test]
    fn selector_window_follows_and_clamps() {
        // short list fits: no scroll offset
        assert_eq!(selector_follow(0, 3, 18, 5), 0);
        // moving down past the window pushes top
        assert_eq!(selector_follow(0, 18, 18, 67), 1);
        assert_eq!(selector_follow(1, 66, 18, 67), 49);
        // moving up above the window pulls top back
        assert_eq!(selector_follow(49, 10, 18, 67), 10);
        // highlight inside window: top unchanged
        assert_eq!(selector_follow(5, 10, 18, 67), 5);
        // visible rows for modal heights (height min(items+6, 24), chrome 6)
        assert_eq!(selector_visible(67), 18);
        assert_eq!(selector_visible(3), 3);
        assert_eq!(selector_visible(0), 1);
    }
    #[test]
    fn protocol_round_trip() {
        let py = vec![
            PyMsg::AddMessage { role: "user".into(), content: "hi".into() },
            PyMsg::Update { sidebar: Sidebar::default() },
            PyMsg::SetWorking { working: true, message: "x".into() },
            PyMsg::Clear,
            PyMsg::PromptSelect { title: "t".into(), items: vec!["a".into()] },
            PyMsg::SetCompletions { items: vec!["/help".into()] },
            PyMsg::System { content: "s".into() },
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
            RustMsg::ModeCycle { mode: "Build Mode".into() },
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
    fn completion_filter_and_accept() {
        let items = vec![
            "/help".to_string(),
            "/plan".into(),
            "/build".into(),
            "/team".into(),
            "/test".into(),
        ];
        assert_eq!(completion_word("/he"), Some("he"));
        assert_eq!(completion_word("/"), Some(""));
        assert_eq!(completion_word("hello"), None);
        assert_eq!(completion_word("/he lp"), None);
        // empty word matches everything (popup lists all)
        assert_eq!(filter_completions(&items, ""), vec![0, 1, 2, 3, 4]);
        assert_eq!(filter_completions(&items, "he"), vec![0]);
        assert_eq!(filter_completions(&items, "TE"), vec![3, 4]);
        assert_eq!(filter_completions(&items, "zzz"), Vec::<usize>::new());
        assert_eq!(popup_matches(&items, "/t"), vec![3, 4]);
        assert_eq!(popup_matches(&items, "plain"), Vec::<usize>::new());
        assert_eq!(accept_completion("/he", "/help"), "/help");
        assert_eq!(accept_completion("/", "/help"), "/help");
    }
    #[test]
    fn table_and_tasklist() {
        let t = plain(&render_md("| a | b |\n|---|---|\n| c | d |\n", 40));
        assert!(t.contains('a') && t.contains('d'), "{t}");
        assert!(t.contains('\u{2502}'), "{t}");
        let t2 = plain(&render_md("- [x] done\n- [ ] todo\n", 40));
        assert!(t2.contains('\u{2713}') && t2.contains('\u{25cb}'), "{t2}");
    }
    #[test]
    fn wrap_preserves_bold_and_chunks_long_words() {
        let lines = render_md("**boldword** https://example.com/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 20);
        assert!(lines.iter().all(|l| l.width() <= 20), "{}", plain(&lines));
        let all_bold = lines.iter().flat_map(|l| l.spans.iter()).any(|s| {
            s.content.contains("boldword")
                && s.style.add_modifier.contains(Modifier::BOLD)
        });
        assert!(all_bold, "{}", plain(&lines));
    }
    #[test]
    fn ascii_emit_and_clean_text() {
        let j = serde_json::to_string(&RustMsg::Input { text: "é😀".into() }).unwrap();
        let a = ascii_json(&j);
        assert!(a.is_ascii(), "{a}");
        let back: RustMsg = serde_json::from_str(&a).unwrap();
        assert_eq!(serde_json::to_string(&back).unwrap(), j);
        assert_eq!(clean_text("\x1b[31mred\x1b[0m\tx\r\ny\x07"), "red    x\ny");
    }
    #[test]
    fn wide_chars_respect_display_width() {
        let lines = render_md("\u{3042}".repeat(30).as_str(), 20);
        assert!(lines.iter().all(|l| l.width() <= 20), "{}", plain(&lines));
        assert_eq!(trunc("\u{3042}\u{3042}\u{3042}\u{3042}\u{3042}", 8), "\u{3042}\u{3042}\u{3042}…");
    }
}
