//! Drawing: conversation (gutters, no boxes), Nim board, sidebar, input.
use crate::app::{App, Cache, Msg};
use crate::theme::{mode_word, Theme, ASH, BONE, ERROR, GRAPHITE, PINE, SULFUR, WISTERIA};
use pulldown_cmark::{Event as MdEv, Options, Parser, Tag, TagEnd};
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    Frame,
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

pub const SIDEBAR_W: u16 = 30;
/// Below this width the sidebar hides and the status line carries "41k/128k".
pub const SIDEBAR_MIN_COLS: u16 = 90;
const TEXT_MAX: usize = 100;
const FOLD_OVER: usize = 12;
const FOLD_HEAD: usize = 6;
const FOLD_TAIL: usize = 2;
const MAX_INPUT_ROWS: usize = 6;
const HEAPS: [usize; 4] = [1, 3, 5, 7];
const STICKS: u64 = 16;

// ---------- markdown ----------

#[derive(Default, Clone)]
struct Sty {
    bold: bool,
    italic: bool,
    code: bool,
    block: bool,
    heading: bool,
    quote: bool,
    link: bool,
    strike: bool,
}

impl Sty {
    fn style(&self, th: &Theme, base: Style) -> Style {
        let mut s = base;
        if self.block || self.quote {
            s = s.patch(th.fg(ASH));
        } else if self.code {
            s = s.patch(th.fg(BONE));
            if th.color {
                s = s.bg(GRAPHITE);
            }
        } else if self.heading {
            s = s.patch(th.fg(BONE));
        }
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
        s
    }
}

/// Parse markdown into styled lines (unwrapped).
pub fn markdown_lines(md: &str, th: &Theme, base: Style) -> Vec<Line<'static>> {
    let g = th.g();
    let mut opts = Options::empty();
    opts.insert(Options::ENABLE_STRIKETHROUGH);
    opts.insert(Options::ENABLE_TABLES);
    opts.insert(Options::ENABLE_TASKLISTS);
    let mut out: Vec<Line<'static>> = vec![];
    let mut cur: Vec<Span<'static>> = vec![];
    let mut sty = Sty::default();
    let mut list_stack: Vec<Option<u64>> = vec![];
    let mut prefix: Option<Span<'static>> = None;
    let quiet = base.patch(th.fg(ASH));

    let flush = |cur: &mut Vec<Span<'static>>, out: &mut Vec<Line<'static>>| {
        out.push(Line::from(std::mem::take(cur)));
    };

    for ev in Parser::new_ext(md, opts) {
        match ev {
            MdEv::Start(tag) => match tag {
                Tag::Heading { .. } => {
                    if !cur.is_empty() {
                        flush(&mut cur, &mut out);
                    }
                    sty.heading = true;
                }
                Tag::Strong => sty.bold = true,
                Tag::Emphasis => sty.italic = true,
                Tag::Strikethrough => sty.strike = true,
                Tag::Link { .. } => sty.link = true,
                Tag::CodeBlock(_) => {
                    if !cur.is_empty() {
                        flush(&mut cur, &mut out);
                    }
                    sty.block = true;
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
                        cur.push(Span::styled(format!(" {} ", g.cell), th.fg(GRAPHITE)));
                    }
                }
                Tag::Item => {
                    if !cur.is_empty() {
                        flush(&mut cur, &mut out);
                    }
                    let depth = list_stack.len().saturating_sub(1);
                    let marker = match list_stack.last() {
                        Some(Some(n)) => format!("{n}."),
                        _ => g.bullet.to_string(),
                    };
                    if let Some(Some(n)) = list_stack.last_mut() {
                        *n += 1;
                    }
                    prefix = Some(Span::styled(
                        format!("{}{} ", "  ".repeat(depth), marker),
                        quiet,
                    ));
                }
                Tag::Paragraph if !cur.is_empty() => flush(&mut cur, &mut out),
                _ => {}
            },
            MdEv::End(end) => match end {
                TagEnd::Heading(_) => {
                    flush(&mut cur, &mut out);
                    out.push(Line::from(""));
                    sty.heading = false;
                }
                TagEnd::Strong => sty.bold = false,
                TagEnd::Emphasis => sty.italic = false,
                TagEnd::Strikethrough => sty.strike = false,
                TagEnd::Link => sty.link = false,
                TagEnd::CodeBlock => {
                    if !cur.is_empty() {
                        flush(&mut cur, &mut out);
                    }
                    out.push(Line::from(""));
                    sty.block = false;
                }
                TagEnd::BlockQuote(_) => sty.quote = false,
                TagEnd::List(_) => {
                    list_stack.pop();
                    if list_stack.is_empty() {
                        out.push(Line::from(""));
                    }
                }
                TagEnd::TableRow | TagEnd::TableHead => flush(&mut cur, &mut out),
                TagEnd::Item => flush(&mut cur, &mut out),
                // blank line after top-level blocks (trailing ones trimmed below)
                TagEnd::Paragraph | TagEnd::Table => {
                    flush(&mut cur, &mut out);
                    if list_stack.is_empty() && !sty.quote {
                        out.push(Line::from(""));
                    }
                }
                _ => {}
            },
            MdEv::Text(t) => {
                let s = t.into_string();
                let s = if sty.block {
                    s.strip_suffix('\n').unwrap_or(&s).to_string()
                } else {
                    s
                };
                for (i, part) in s.split('\n').enumerate() {
                    if i > 0 {
                        flush(&mut cur, &mut out);
                    }
                    if let Some(p) = prefix.take() {
                        cur.push(p);
                    }
                    if cur.is_empty() && sty.quote {
                        cur.push(Span::styled(format!("{} ", g.quote), th.fg(GRAPHITE)));
                    }
                    if cur.is_empty() && sty.block {
                        cur.push(Span::raw("  "));
                    }
                    if !part.is_empty() {
                        cur.push(Span::styled(part.to_string(), sty.style(th, base)));
                    }
                }
            }
            MdEv::Code(c) => {
                let mut cs = sty.clone();
                cs.code = true;
                let text = if th.color {
                    format!(" {c} ")
                } else {
                    format!("`{c}`")
                };
                if let Some(p) = prefix.take() {
                    cur.push(p);
                }
                cur.push(Span::styled(text, cs.style(th, base)));
            }
            MdEv::SoftBreak | MdEv::HardBreak => flush(&mut cur, &mut out),
            MdEv::Rule => out.push(Line::from(Span::styled(
                g.hrule.repeat(24),
                th.fg(GRAPHITE),
            ))),
            MdEv::TaskListMarker(done) => {
                prefix = None;
                cur.push(Span::styled(
                    format!("{} ", if done { g.done } else { g.open }),
                    quiet,
                ));
            }
            _ => {}
        }
    }
    if !cur.is_empty() {
        out.push(Line::from(cur));
    }
    // one blank line between blocks, none at the ends
    out.dedup_by(|a, b| a.width() == 0 && b.width() == 0);
    if out.first().is_some_and(|l| l.width() == 0) {
        out.remove(0);
    }
    while out.len() > 1 && out.last().is_some_and(|l| l.width() == 0) {
        out.pop();
    }
    if out.is_empty() {
        out.push(Line::from(""));
    }
    out
}

/// Greedy char-aware wrap that keeps span styles.
pub fn wrap_lines(src: Vec<Line<'static>>, width: usize) -> Vec<Line<'static>> {
    let w = width.max(1);
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
            // a space that would start a continuation row is dropped
            if len + ww > w && len > 0 && word.len() == 1 && word[0].0 == ' ' {
                push_run(&mut run, run_style, &mut cur);
                out.push(Line::from(std::mem::take(&mut cur)));
                len = 0;
                continue;
            }
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

pub fn trunc(s: &str, n: usize, ell: &str) -> String {
    if s.width() <= n {
        return s.into();
    }
    let room = n.saturating_sub(ell.width());
    let mut out = String::new();
    let mut w = 0usize;
    for ch in s.chars() {
        let cw = ch.width().unwrap_or(0);
        if w + cw > room {
            break;
        }
        out.push(ch);
        w += cw;
    }
    out + ell
}

/// Keep the tail (paths): "…/projects/nimcode".
fn trunc_left(s: &str, n: usize, ell: &str) -> String {
    if s.width() <= n {
        return s.into();
    }
    let room = n.saturating_sub(ell.width());
    let mut tail: Vec<char> = vec![];
    let mut w = 0usize;
    for ch in s.chars().rev() {
        let cw = ch.width().unwrap_or(0);
        if w + cw > room {
            break;
        }
        tail.push(ch);
        w += cw;
    }
    format!("{ell}{}", tail.into_iter().rev().collect::<String>())
}

// ---------- messages ----------

fn role_color(m: &Msg) -> ratatui::style::Color {
    match m.role.as_str() {
        "user" => WISTERIA,
        "assistant" => PINE,
        "tool" => SULFUR,
        _ if m.content.starts_with("Error") => ERROR,
        _ if m.content.starts_with("Warning") => SULFUR,
        _ => ASH,
    }
}

/// "output (412 chars)" -> ("output", "412 chars").
fn split_paren(s: &str) -> (String, String) {
    match (s.find(" ("), s.ends_with(')')) {
        (Some(i), true) => (s[..i].to_string(), s[i + 2..s.len() - 1].to_lowercase()),
        _ => (s.to_string(), String::new()),
    }
}

/// Tool messages arrive as "Preview:\n[Tool Preview: write_file] Path: x\n<diff>",
/// "Output (N chars):\n<result>", "Tool Call: name" — lift the header into a label.
pub fn tool_parts(content: &str) -> (String, String, Vec<&str>) {
    let mut lines: Vec<&str> = content.lines().collect();
    let first = lines.first().map(|l| l.trim()).unwrap_or("");
    if let Some(name) = first.strip_prefix("Tool Call: ") {
        return (name.to_string(), String::new(), lines.split_off(1));
    }
    let (mut name, mut sub) = ("tool".to_string(), String::new());
    if first.ends_with(':') && first.width() <= 40 {
        (name, sub) = split_paren(&first[..first.len() - 1].to_lowercase());
        lines.remove(0);
    }
    while lines.first().is_some_and(|l| l.trim().is_empty()) {
        lines.remove(0);
    }
    if let Some(rest) = lines
        .first()
        .and_then(|l| l.strip_prefix("[Tool Preview: "))
    {
        if let Some(close) = rest.find(']') {
            let (n, kind) = split_paren(&rest[..close]);
            let tail = rest[close + 1..].trim();
            let tail = tail.strip_prefix("Path: ").unwrap_or(tail);
            name = n;
            sub = [kind.as_str(), tail]
                .iter()
                .filter(|s| !s.is_empty())
                .cloned()
                .collect::<Vec<_>>()
                .join("  ");
            lines.remove(0);
        }
    }
    while lines.last().is_some_and(|l| l.trim().is_empty()) {
        lines.pop();
    }
    (name, sub, lines)
}

fn tool_line_style(l: &str, th: &Theme) -> Style {
    if l.starts_with("+++") || l.starts_with("---") || l.starts_with("@@") {
        th.fg(ASH)
    } else if l.starts_with('+') {
        th.fg(PINE)
    } else if l.starts_with('-') {
        th.fg(SULFUR)
    } else if l.starts_with("Error") || l.starts_with("Traceback") || l.starts_with("error:") {
        th.fg(ERROR)
    } else {
        th.fg(ASH)
    }
}

fn label_line(name: &str, color: ratatui::style::Color, sub: &str, th: &Theme) -> Line<'static> {
    let mut spans = vec![Span::styled(
        name.to_string(),
        th.fg(color).add_modifier(Modifier::BOLD),
    )];
    if !sub.is_empty() {
        spans.push(Span::styled(format!("  {sub}"), th.fg(ASH)));
    }
    Line::from(spans)
}

pub fn render_msg(m: &Msg, width: usize, fold: bool, th: &Theme) -> Vec<Line<'static>> {
    let g = th.g();
    let mut out = vec![];
    let color = role_color(m);
    match m.role.as_str() {
        "tool" => {
            let (name, sub, body) = tool_parts(&m.content);
            out.push(label_line(
                &name,
                SULFUR,
                &trunc(&sub, width.saturating_sub(name.width() + 2), g.ellipsis),
                th,
            ));
            let n = body.len();
            let styled = |l: &str| Line::from(Span::styled(l.to_string(), tool_line_style(l, th)));
            let mut src: Vec<Line<'static>> = vec![];
            if fold && n > FOLD_OVER {
                src.extend(body[..FOLD_HEAD].iter().map(|l| styled(l)));
                src.push(Line::from(vec![
                    Span::styled(
                        format!("{} {} more lines", g.ellipsis, n - FOLD_HEAD - FOLD_TAIL),
                        th.fg(BONE),
                    ),
                    Span::styled("  ctrl+o to expand", th.fg(ASH)),
                ]));
                src.extend(body[n - FOLD_TAIL..].iter().map(|l| styled(l)));
            } else {
                src.extend(body.iter().map(|l| styled(l)));
            }
            out.extend(wrap_lines(src, width));
        }
        "user" => {
            if m.label {
                out.push(label_line("you", WISTERIA, "", th));
            }
            let src = m
                .content
                .split('\n')
                .map(|l| Line::from(Span::styled(l.to_string(), th.fg(BONE))))
                .collect();
            out.extend(wrap_lines(src, width));
        }
        role => {
            let base = if role == "assistant" || m.content.contains("(y/n") {
                th.fg(BONE)
            } else {
                th.fg(ASH)
            };
            if m.label && role != "system" {
                let name = if role == "assistant" { "nimcode" } else { role };
                out.push(label_line(name, color, "", th));
            }
            out.extend(wrap_lines(
                markdown_lines(m.content.trim(), th, base),
                width,
            ));
        }
    }
    out
}

fn ensure_cache(m: &mut Msg, width: usize, fold: bool, th: &Theme) -> usize {
    let folded = fold && m.role == "tool";
    let ok = m
        .cache
        .as_ref()
        .is_some_and(|c| c.width == width && c.folded == folded);
    if !ok {
        m.cache = Some(Cache {
            width,
            folded,
            lines: render_msg(m, width, fold, th),
        });
    }
    m.cache.as_ref().map_or(0, |c| c.lines.len()) + m.gap as usize
}

// ---------- Nim board ----------

pub fn struck_sticks(used: u64, budget: u64) -> u64 {
    if budget == 0 {
        return 0;
    }
    ((used as u128 * STICKS as u128 + budget as u128 / 2) / budget as u128).min(STICKS as u128)
        as u64
}

/// Four heaps of 1-3-5-7 matches; each stick = 1/16 of the budget. Struck
/// sticks lose their sulfur head and dim to graphite, top heap first,
/// right to left. Every row is exactly the board width (centerable).
pub fn board(used: u64, budget: u64, th: &Theme, large: bool) -> Vec<Line<'static>> {
    let g = th.g();
    let s = if large { 3 } else { 2 };
    let w = 6 * s + 1;
    let struck = struck_sticks(used, budget) as usize;
    let mut rows = vec![];
    let mut before = 0usize;
    for (hi, &n) in HEAPS.iter().enumerate() {
        let left = (7 - n) / 2 * s;
        let is_struck = |k: usize| before + (n - 1 - k) < struck;
        let row = |head: bool| {
            let mut spans = vec![];
            let mut col = 0;
            for k in 0..n {
                let c = left + k * s;
                spans.push(Span::raw(" ".repeat(c - col)));
                let (glyph, color) = match (head, is_struck(k)) {
                    (true, false) => (g.head, SULFUR),
                    (true, true) => (" ", GRAPHITE),
                    (false, false) => (g.stick, BONE),
                    (false, true) => (g.struck, GRAPHITE),
                };
                spans.push(Span::styled(glyph, th.fg(color)));
                col = c + 1;
            }
            spans.push(Span::raw(" ".repeat(w - col)));
            Line::from(spans)
        };
        rows.push(row(true));
        for _ in 0..(if large { 2 } else { 1 }) {
            rows.push(row(false));
        }
        if large && hi < HEAPS.len() - 1 {
            rows.push(Line::from(" ".repeat(w)));
        }
        before += n;
    }
    rows
}

pub fn fmt_tokens(n: u64) -> String {
    let short = |v: f64, unit: &str| {
        let s = format!("{v:.1}");
        format!("{}{unit}", s.strip_suffix(".0").unwrap_or(&s))
    };
    match n {
        0..=999 => n.to_string(),
        1000..=9_999 => short(n as f64 / 1000.0, "k"),
        10_000..=999_999 => format!("{}k", (n + 500) / 1000),
        _ => short(n as f64 / 1_000_000.0, "M"),
    }
}

fn token_color(used: u64, budget: u64) -> ratatui::style::Color {
    if budget > 0 && used * 4 >= budget * 3 {
        SULFUR
    } else {
        ASH
    }
}

/// Burning fuse: shortens frame by frame, then relights. Fixed 9 columns.
fn fuse(frame: usize, th: &Theme) -> Vec<Span<'static>> {
    let g = th.g();
    const L: usize = 8;
    let burnt = frame % (L + 1);
    vec![
        Span::styled(g.fuse.repeat(L - burnt), th.fg(ASH)),
        Span::styled(g.fuse_tip, th.fg(SULFUR).add_modifier(Modifier::BOLD)),
        Span::raw(" ".repeat(burnt)),
    ]
}

fn short_cwd(cwd: &str) -> String {
    for var in ["USERPROFILE", "HOME"] {
        if let Ok(home) = std::env::var(var) {
            if !home.is_empty() && cwd.to_lowercase().starts_with(&home.to_lowercase()) {
                return format!("~{}", &cwd[home.len()..]);
            }
        }
    }
    cwd.to_string()
}

// ---------- input layout ----------

/// Soft-wrap the input into rows of `w` columns; returns rows and the
/// cursor's (row, col).
pub fn input_rows(input: &str, cursor: usize, w: usize) -> (Vec<String>, (usize, usize)) {
    let w = w.max(1);
    let mut rows = vec![String::new()];
    let mut col = 0usize;
    let mut cur = None;
    for (i, ch) in input.char_indices() {
        if ch == '\n' {
            if i == cursor {
                cur = Some((rows.len() - 1, col));
            }
            rows.push(String::new());
            col = 0;
            continue;
        }
        let cw = ch.width().unwrap_or(0);
        if col + cw > w {
            rows.push(String::new());
            col = 0;
        }
        if i == cursor {
            cur = Some((rows.len() - 1, col));
        }
        rows.last_mut().unwrap().push(ch);
        col += cw;
    }
    let cur = cur.unwrap_or((rows.len() - 1, col));
    (rows, cur)
}

// ---------- drawing ----------

fn put(f: &mut Frame, x: u16, y: u16, line: &Line, max_w: u16) {
    let area = f.area();
    if y < area.y + area.height && x < area.x + area.width {
        let room = (area.x + area.width - x).min(max_w);
        f.buffer_mut().set_line(x, y, line, room);
    }
}

fn hint_line(app: &App) -> Line<'static> {
    let th = &app.theme;
    let g = th.g();
    let q = |s: String| Span::styled(s, th.fg(ASH));
    let sep = g.sep;
    let text = if app.selector.is_some() {
        format!(
            "{}{} move {sep} enter choose {sep} esc cancel",
            g.up, g.down
        )
    } else if !app.popup().is_empty() {
        format!("tab complete {sep} esc dismiss")
    } else if !app.input.is_empty() {
        let nl = if app.shift_enter {
            "shift+enter"
        } else {
            "ctrl+j"
        };
        format!("enter send {sep} {nl} newline")
    } else if app
        .msgs
        .back()
        .is_some_and(|m| m.role == "system" && m.content.contains("(y/n"))
    {
        format!("y approve {sep} n skip")
    } else if !app.stick {
        "esc back to latest".to_string()
    } else if app.sidebar.is_working {
        "ctrl+c stop".to_string()
    } else {
        // the mode cycle, current mode lit in its colour
        let cur = mode_word(&app.sidebar.mode);
        let mut spans = vec![q("shift+tab  ".into())];
        for (i, m) in ["Plan", "Build"].iter().enumerate() {
            if i > 0 {
                spans.push(q(format!(" {sep} ")));
            }
            let lit = *m == cur;
            let st = if lit {
                th.fg(Theme::mode_color(m)).add_modifier(Modifier::BOLD)
            } else {
                th.fg(ASH)
            };
            spans.push(Span::styled(m.to_lowercase(), st));
        }
        return Line::from(spans);
    };
    Line::from(q(text))
}

pub fn draw(f: &mut Frame, app: &mut App) {
    let th = app.theme;
    let g = th.g();
    let area = f.area();
    f.buffer_mut().set_style(area, th.base());
    if area.width < 20 || area.height < 6 {
        put(
            f,
            area.x,
            area.y,
            &Line::from("nimcode: enlarge the terminal"),
            area.width,
        );
        return;
    }
    let right = area.x + area.width;
    let iw = area.width.saturating_sub(4) as usize;
    let (rows, (crow, ccol)) = input_rows(&app.input, app.cursor, iw);
    let ih = rows.len().clamp(1, MAX_INPUT_ROWS) as u16;
    let empty = app.is_empty_state() && app.selector.is_none();
    let side = !empty && area.width >= SIDEBAR_MIN_COLS;
    // the status row exists only when it has something to say
    let status = app.sidebar.is_working || (!empty && (!app.stick || !side));
    let body_h = area.height.saturating_sub(1 + status as u16 + ih);
    let rule_x = right - SIDEBAR_W - 1;
    let conv_right = if side { rule_x - 1 } else { right - 1 };
    let conv = Rect {
        x: area.x + 1,
        y: area.y + 1,
        width: conv_right - area.x - 1,
        height: body_h.saturating_sub(1),
    };

    if app.selector.is_some() {
        draw_selector(f, app, conv);
    } else if empty {
        draw_empty(
            f,
            app,
            Rect {
                height: body_h,
                ..area
            },
        );
    } else {
        draw_conv(f, app, conv);
    }

    // one vertical rule, one horizontal rule, meeting in a junction
    let rule_st = th.fg(GRAPHITE);
    if side {
        for y in area.y..area.y + body_h {
            put(f, rule_x, y, &Line::from(Span::styled(g.vrule, rule_st)), 1);
        }
        draw_sidebar(
            f,
            app,
            Rect {
                x: rule_x + 3,
                y: area.y + 1,
                width: SIDEBAR_W - 3,
                height: body_h.saturating_sub(1),
            },
        );
    }
    let ry = area.y + body_h;
    put(
        f,
        area.x,
        ry,
        &Line::from(Span::styled(g.hrule.repeat(area.width as usize), rule_st)),
        area.width,
    );
    if side {
        put(
            f,
            rule_x,
            ry,
            &Line::from(Span::styled(g.junction, rule_st)),
            1,
        );
    }

    // status line: fuse + working message left; scroll / tokens right
    let sy = ry + status as u16;
    let sb = &app.sidebar;
    let mut right_parts: Vec<Span> = vec![];
    if app.below > 0 && app.selector.is_none() && !empty {
        right_parts.push(Span::styled(
            format!("{} {} more", g.up, app.below),
            th.fg(BONE),
        ));
    }
    if !side && !empty {
        if !right_parts.is_empty() {
            right_parts.push(Span::raw("   "));
        }
        right_parts.push(Span::styled(
            format!(
                "{}/{}",
                fmt_tokens(sb.tokens_used),
                fmt_tokens(sb.tokens_budget)
            ),
            th.fg(token_color(sb.tokens_used, sb.tokens_budget)),
        ));
    }
    let rline = Line::from(right_parts);
    let rw = rline.width() as u16;
    let rx = right.saturating_sub(1 + rw);
    let mut left: Vec<Span> = vec![];
    if sb.is_working {
        left.extend(fuse(app.spinner, &th));
        left.push(Span::styled(
            format!("  {}", sb.working_message.trim()),
            th.fg(ASH),
        ));
        if let Some(t) = app.working_since {
            let secs = t.elapsed().as_secs();
            if secs >= 1 {
                left.push(Span::styled(format!("  {secs}s"), th.fg(GRAPHITE)));
            }
        }
    } else if !side && !empty {
        left.push(Span::styled(sb.model.clone(), th.fg(ASH)));
    }
    if status {
        put(
            f,
            area.x + 1,
            sy,
            &Line::from(left),
            rx.saturating_sub(area.x + 2),
        );
        put(f, rx, sy, &rline, rw);
    }

    // input: "› " in the mode colour, then the text; hint right-aligned
    let iy = ry + 1 + status as u16;
    let start = crow.saturating_sub(ih as usize - 1);
    let pst = if app.selector.is_some() {
        th.fg(ASH)
    } else {
        th.fg(Theme::mode_color(&sb.mode))
            .add_modifier(Modifier::BOLD)
    };
    put(
        f,
        area.x + 1,
        iy,
        &Line::from(Span::styled(g.prompt, pst)),
        1,
    );
    for (i, row) in rows.iter().skip(start).take(ih as usize).enumerate() {
        put(
            f,
            area.x + 3,
            iy + i as u16,
            &Line::from(Span::styled(row.clone(), th.fg(BONE))),
            iw as u16 + 1,
        );
    }
    let hint = hint_line(app);
    let hw = hint.width() as u16;
    let used_w = rows.first().map_or(0, |r| r.width()) as u16;
    if start == 0 && 3 + used_w + 3 + hw < area.width {
        put(f, right - 1 - hw, iy, &hint, hw);
    }
    if app.selector.is_none() {
        f.set_cursor_position((area.x + 3 + ccol as u16, iy + (crow - start) as u16));
    }

    draw_popup(f, app, area.x, conv_right, ry);
}

fn draw_conv(f: &mut Frame, app: &mut App, conv: Rect) {
    let th = app.theme;
    let g = th.g();
    let tw = (conv.width as usize).saturating_sub(3).clamp(1, TEXT_MAX);
    let fold = app.fold;
    let mut total = 0usize;
    for m in app.msgs.iter_mut() {
        total += ensure_cache(m, tw, fold, &th);
    }
    // a leading gap would waste the first row
    if app.msgs.front().is_some_and(|m| m.gap) {
        app.msgs[0].gap = false;
        total -= 1;
    }
    let h = conv.height as usize;
    app.view_h = h;
    app.max_top = total.saturating_sub(h);
    if app.stick || app.top >= app.max_top {
        app.top = app.max_top;
        app.stick = true;
    }
    let top = app.top;
    app.below = total.saturating_sub(top + h);
    let flash = if app.flashing() {
        app.msgs.iter().rposition(|m| m.role == "assistant")
    } else {
        None
    };
    let mut y = 0usize;
    for (i, m) in app.msgs.iter().enumerate() {
        let lines = &m.cache.as_ref().unwrap().lines;
        let mh = m.gap as usize + lines.len();
        if y + mh <= top {
            y += mh;
            continue;
        }
        if y >= top + h {
            break;
        }
        y += m.gap as usize;
        let color = if flash == Some(i) {
            SULFUR
        } else {
            role_color(m)
        };
        let gut = Line::from(Span::styled(g.gutter, th.fg(color)));
        for line in lines {
            if y >= top && y < top + h {
                let row = conv.y + (y - top) as u16;
                put(f, conv.x, row, &gut, 1);
                put(f, conv.x + 2, row, line, tw as u16);
            }
            y += 1;
        }
    }
}

fn center_x(area: Rect, w: usize) -> u16 {
    area.x + area.width.saturating_sub(w as u16) / 2
}

fn draw_empty(f: &mut Frame, app: &mut App, body: Rect) {
    let th = app.theme;
    let sb = &app.sidebar;
    let notes: Vec<&Msg> = app.msgs.iter().rev().take(2).collect();
    let extra = 2 + if notes.is_empty() { 0 } else { 1 + notes.len() };
    let mut lines = if (body.height as usize) >= 15 + extra + 2 {
        board(sb.tokens_used, sb.tokens_budget, &th, true)
    } else if (body.height as usize) > 8 + extra {
        board(sb.tokens_used, sb.tokens_budget, &th, false)
    } else {
        vec![]
    };
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "Describe a task, or type / for commands.",
        th.fg(ASH),
    )));
    if !notes.is_empty() {
        lines.push(Line::from(""));
        for m in notes.into_iter().rev() {
            let first = m.content.trim().lines().next().unwrap_or("").to_string();
            let text = trunc(
                &first,
                body.width.saturating_sub(4) as usize,
                th.g().ellipsis,
            );
            lines.push(Line::from(Span::styled(text, th.fg(ASH))));
        }
    }
    let y0 = body.y + body.height.saturating_sub(lines.len() as u16) / 2;
    for (i, l) in lines.iter().enumerate() {
        let y = y0 + i as u16;
        if y >= body.y + body.height {
            break;
        }
        put(f, center_x(body, l.width()), y, l, body.width);
    }
}

fn draw_sidebar(f: &mut Frame, app: &App, r: Rect) {
    let th = app.theme;
    let g = th.g();
    let sb = &app.sidebar;
    let w = r.width as usize;
    let quiet = th.fg(ASH);
    let mut lines = board(sb.tokens_used, sb.tokens_budget, &th, false);
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        format!(
            "{} of {}",
            fmt_tokens(sb.tokens_used),
            fmt_tokens(sb.tokens_budget)
        ),
        th.fg(token_color(sb.tokens_used, sb.tokens_budget)),
    )));
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        mode_word(&sb.mode).to_string(),
        th.fg(Theme::mode_color(&sb.mode))
            .add_modifier(Modifier::BOLD),
    )));
    lines.push(Line::from(Span::styled(
        trunc(&sb.model, w, g.ellipsis),
        th.fg(BONE),
    )));
    lines.push(Line::from(Span::styled(
        trunc_left(&short_cwd(&sb.cwd), w, g.ellipsis),
        quiet,
    )));

    let more = |n: usize| Line::from(Span::styled(format!(" + {n} more"), quiet));
    let section =
        |lines: &mut Vec<Line<'static>>, label: &str, items: Vec<Line<'static>>, cap: usize| {
            if items.is_empty() {
                return;
            }
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(label.to_string(), quiet)));
            let n = items.len();
            lines.extend(items.into_iter().take(cap));
            if n > cap {
                lines.push(more(n - cap));
            }
        };
    let todos = sb
        .todos
        .iter()
        .map(|t| {
            let (glyph, st) = if t.done {
                (g.done, quiet)
            } else {
                (g.open, th.fg(BONE))
            };
            Line::from(vec![
                Span::styled(format!(" {glyph} "), quiet),
                Span::styled(trunc(&t.text, w.saturating_sub(3), g.ellipsis), st),
            ])
        })
        .collect();
    section(&mut lines, "todo", todos, 8);
    let agents = sb
        .subagents
        .iter()
        .map(|a| {
            let status = a.status.to_lowercase();
            let st = match status.as_str() {
                "failed" | "error" => th.fg(ERROR),
                "timed_out" => th.fg(SULFUR),
                _ => quiet,
            };
            let name = trunc(&a.role, w.saturating_sub(status.width() + 3), g.ellipsis);
            let pad = w.saturating_sub(1 + name.width() + status.width());
            Line::from(vec![
                Span::styled(format!(" {name}"), th.fg(BONE)),
                Span::raw(" ".repeat(pad)),
                Span::styled(status, st),
            ])
        })
        .collect();
    section(&mut lines, "agents", agents, 5);
    let procs = sb
        .procs
        .iter()
        .map(|p| {
            Line::from(vec![
                Span::styled(format!(" {}  ", p.pid), quiet),
                Span::styled(
                    trunc(&p.command, w.saturating_sub(p.pid.width() + 3), g.ellipsis),
                    th.fg(BONE),
                ),
            ])
        })
        .collect();
    section(&mut lines, "processes", procs, 5);

    for (i, l) in lines.iter().take(r.height as usize).enumerate() {
        put(f, r.x, r.y + i as u16, l, r.width);
    }
}

fn draw_selector(f: &mut Frame, app: &mut App, conv: Rect) {
    let th = app.theme;
    let g = th.g();
    let rows = (conv.height as usize).saturating_sub(3).max(1);
    app.sel_rows = rows;
    let mode = Theme::mode_color(&app.sidebar.mode);
    let Some(sel) = app.selector.as_mut() else {
        return;
    };
    let n = sel.items.len();
    sel.top = crate::app::selector_follow(sel.top, sel.idx, rows, n);
    let w = conv.width.saturating_sub(2);
    put(
        f,
        conv.x,
        conv.y,
        &Line::from(Span::styled(g.gutter, th.fg(WISTERIA))),
        1,
    );
    put(
        f,
        conv.x + 2,
        conv.y,
        &Line::from(Span::styled(
            sel.title.to_lowercase(),
            th.fg(BONE).add_modifier(Modifier::BOLD),
        )),
        w,
    );
    if n == 0 {
        put(
            f,
            conv.x + 2,
            conv.y + 2,
            &Line::from(Span::styled("nothing to choose", th.fg(ASH))),
            w,
        );
    }
    let end = (sel.top + rows).min(n);
    for (j, it) in sel.items[sel.top..end].iter().enumerate() {
        let i = sel.top + j;
        let y = conv.y + 2 + j as u16;
        let text = trunc(it, w.saturating_sub(3) as usize, g.ellipsis);
        if i == sel.idx {
            f.buffer_mut().set_style(
                Rect {
                    x: conv.x + 2,
                    y,
                    width: w,
                    height: 1,
                },
                th.selected(),
            );
            let line = Line::from(vec![
                Span::styled(
                    format!("{} ", g.prompt),
                    th.fg(mode).add_modifier(Modifier::BOLD),
                ),
                Span::styled(text, th.fg(BONE)),
            ]);
            put(f, conv.x + 2, y, &line, w);
        } else {
            put(
                f,
                conv.x + 4,
                y,
                &Line::from(Span::styled(text, th.fg(ASH))),
                w - 2,
            );
        }
    }
    let mut more = vec![];
    if sel.top > 0 {
        more.push(format!("{} {} more", g.up, sel.top));
    }
    if end < n {
        more.push(format!("{} {} more", g.down, n - end));
    }
    if !more.is_empty() {
        let y = conv.y + 2 + (end - sel.top) as u16;
        let line = Line::from(Span::styled(
            more.join(&format!("  {}  ", g.sep)),
            th.fg(ASH),
        ));
        put(f, conv.x + 4, y, &line, w - 2);
    }
}

/// Slash-command completions, laid directly over the conversation's bottom
/// rows (no box: a blank row above separates them).
fn draw_popup(f: &mut Frame, app: &App, x0: u16, x1: u16, rule_y: u16) {
    let th = app.theme;
    let pm = app.popup();
    if pm.is_empty() {
        return;
    }
    let ci = app.comp_idx.min(pm.len() - 1);
    let shown: Vec<usize> = pm
        .iter()
        .skip(ci.saturating_sub(7))
        .take(8)
        .cloned()
        .collect();
    let h = (shown.len() as u16 + 1).min(rule_y);
    let area = Rect {
        x: x0,
        y: rule_y - h,
        width: x1 - x0,
        height: h,
    };
    for y in area.y..area.y + area.height {
        let row = Rect {
            y,
            height: 1,
            ..area
        };
        f.buffer_mut().set_style(row, th.base());
        put(
            f,
            row.x,
            y,
            &Line::from(" ".repeat(row.width as usize)),
            row.width,
        );
    }
    for (j, &mi) in shown
        .iter()
        .enumerate()
        .skip((shown.len() + 1).saturating_sub(h as usize))
    {
        let y = area.y + 1 + j as u16;
        let active = mi == pm[ci];
        let st = if active { th.fg(BONE) } else { th.fg(ASH) };
        if active {
            f.buffer_mut().set_style(
                Rect {
                    x: x0 + 1,
                    y,
                    width: area.width.saturating_sub(1),
                    height: 1,
                },
                th.selected(),
            );
        }
        let text = trunc(
            &app.completions[mi],
            area.width.saturating_sub(5) as usize,
            th.g().ellipsis,
        );
        put(
            f,
            x0 + 3,
            y,
            &Line::from(Span::styled(text, st)),
            area.width.saturating_sub(4),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Proc, PyMsg, Sub, Todo};
    use ratatui::{backend::TestBackend, Terminal};

    const TH: Theme = Theme {
        color: true,
        ascii: false,
    };

    fn render_md(md: &str, w: usize) -> Vec<Line<'static>> {
        wrap_lines(markdown_lines(md, &TH, Style::default()), w)
    }
    fn plain(lines: &[Line]) -> String {
        lines
            .iter()
            .map(|l| {
                l.spans
                    .iter()
                    .map(|s| s.content.to_string())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn heading_bold_code_list() {
        let md = "# Hi\n\nHello **bold** and *ital* `code`.\n\n- a\n- b\n\n```\nfn x() {}\n```\n";
        let lines = render_md(md, 40);
        let t = plain(&lines);
        assert!(
            t.contains("Hi") && t.contains("bold") && t.contains("code"),
            "{t}"
        );
        assert!(t.contains("• a") && t.contains("fn x()"), "{t}");
        let h = lines
            .iter()
            .find(|l| l.spans.iter().any(|s| s.content.contains("Hi")))
            .unwrap();
        assert!(h
            .spans
            .iter()
            .any(|s| s.style.add_modifier.contains(Modifier::BOLD)));
    }
    #[test]
    fn wrap_and_wide_chars_respect_width() {
        let lines = render_md("word ".repeat(30).trim(), 20);
        assert!(lines.len() > 3 && lines.iter().all(|l| l.width() <= 20));
        let lines = render_md(&"\u{3042}".repeat(30), 20);
        assert!(lines.iter().all(|l| l.width() <= 20), "{}", plain(&lines));
        assert_eq!(
            trunc("\u{3042}\u{3042}\u{3042}\u{3042}\u{3042}", 8, "…"),
            "\u{3042}\u{3042}\u{3042}…"
        );
        let lines = render_md(
            "**boldword** https://example.com/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            20,
        );
        assert!(lines.iter().all(|l| l.width() <= 20), "{}", plain(&lines));
        assert!(!render_md("", 40).is_empty());
    }
    #[test]
    fn table_and_tasklist() {
        let t = plain(&render_md("| a | b |\n|---|---|\n| c | d |\n", 40));
        assert!(t.contains('a') && t.contains('d') && t.contains('│'), "{t}");
        let t2 = plain(&render_md("- [x] done\n- [ ] todo\n", 40));
        assert!(t2.contains('✓') && t2.contains('○'), "{t2}");
    }
    #[test]
    fn board_strikes_top_heap_first() {
        assert_eq!(struck_sticks(0, 128_000), 0);
        assert_eq!(struck_sticks(64_000, 128_000), 8);
        assert_eq!(struck_sticks(999_999, 128_000), 16);
        assert_eq!(struck_sticks(5, 0), 0);
        let rows: Vec<String> = board(8_000, 128_000, &TH, false)
            .iter()
            .map(|l| plain(std::slice::from_ref(l)))
            .collect();
        // one stick struck: the single top match lost its head
        assert_eq!(rows[0].trim(), "");
        assert_eq!(rows[1].trim(), "╎");
        assert_eq!(rows[3].trim(), "┃ ┃ ┃");
        assert!(rows.iter().all(|r| r.width() == 13));
        assert_eq!(board(0, 1, &TH, true).len(), 15);
        assert_eq!(fmt_tokens(41_200), "41k");
        assert_eq!(fmt_tokens(1_200), "1.2k");
        assert_eq!(fmt_tokens(128_000), "128k");
    }
    #[test]
    fn tool_header_becomes_label_and_folds() {
        let (n, s, b) = tool_parts("Preview:\n\n[Tool Preview: write_file] Path: src/a.py\n+x\n");
        assert_eq!(
            (n.as_str(), s.as_str(), b),
            ("write_file", "src/a.py", vec!["+x"])
        );
        let (n, s, _) = tool_parts("Preview:\n[Tool Preview: execute_command (Host)]\nls\n");
        assert_eq!((n.as_str(), s.as_str()), ("execute_command", "host"));
        let (n, s, b) = tool_parts("Output (12 chars):\nhello");
        assert_eq!(
            (n.as_str(), s.as_str(), b),
            ("output", "12 chars", vec!["hello"])
        );
        let mut a = App::new(TH);
        let body: String = (1..=40).map(|i| format!("l{i}\n")).collect();
        a.push("tool", &format!("Output (9 chars):\n{body}"));
        let m = &a.msgs[0];
        assert_eq!(render_msg(m, 80, true, &TH).len(), 1 + 6 + 1 + 2);
        assert_eq!(render_msg(m, 80, false, &TH).len(), 41);
    }
    #[test]
    fn input_rows_wrap_and_cursor() {
        let (rows, cur) = input_rows("abcdef\nxy", 9, 4);
        assert_eq!(rows, vec!["abcd", "ef", "xy"]);
        assert_eq!(cur, (2, 2));
        let (_, cur) = input_rows("abcdef", 4, 4);
        assert_eq!(cur, (1, 0));
    }

    // ---------- snapshots ----------

    fn snapshot(app: &mut App, w: u16, h: u16) -> String {
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, app)).unwrap();
        let buf = term.backend().buffer();
        let mut s = String::new();
        for y in 0..h {
            let mut row = String::new();
            let mut skip = 0;
            for x in 0..w {
                if skip > 0 {
                    skip -= 1;
                    continue;
                }
                let sym = buf[(x, y)].symbol();
                skip = sym.width().saturating_sub(1);
                row.push_str(sym);
            }
            s.push_str(row.trim_end());
            s.push('\n');
        }
        s
    }

    fn base_app(th: Theme) -> App {
        let mut a = App::new(th);
        a.apply(PyMsg::System {
            content: "nimcode · ~/projects/nimcode".into(),
        });
        a.sidebar.mode = "Build Mode".into();
        a.sidebar.model = "deepseek-v4-flash".into();
        a.sidebar.cwd = "/home/dev/projects/nimcode".into();
        a.sidebar.tokens_used = 41_000;
        a.sidebar.tokens_budget = 128_000;
        a
    }

    fn conversation(th: Theme) -> App {
        let mut a = base_app(th);
        a.push("user", "add a --json flag to `nimcode status`");
        a.push(
            "assistant",
            "I'll read **cli.py** first to see how `status` is wired, then add the flag.",
        );
        a.push(
            "tool",
            "Preview:\n\n[Tool Preview: read_file] Path: src/nimcode/cli.py\n",
        );
        a.push("system", "Executing read_file...");
        let body: String = (1..=40)
            .map(|i| format!("{i:>3}  def line_{i}(): ...\n"))
            .collect();
        a.push("tool", &format!("Output ({} chars):\n{body}", body.len()));
        a.push(
            "assistant",
            "Found it. The plan:\n\n1. add `--json` to `status`\n2. print `json.dumps(cfg)`\n\nThis is a long sentence that should wrap neatly inside the hundred column measure without ever touching the sidebar rule.",
        );
        a.push(
            "tool",
            "Preview:\n\n[Tool Preview: write_file] Path: src/nimcode/cli.py\n--- a/src/nimcode/cli.py\n+++ b/src/nimcode/cli.py\n@@ -1,3 +1,4 @@\n import typer\n-def status():\n+def status(json: bool = False):\n",
        );
        a.push(
            "system",
            "Approve execution of write_file? (y/n to skip) (y/n)",
        );
        a.sidebar.todos = vec![
            Todo {
                text: "read cli.py".into(),
                done: true,
            },
            Todo {
                text: "add --json flag".into(),
                done: false,
            },
        ];
        a.sidebar.subagents = vec![Sub {
            role: "reviewer".into(),
            status: "RUNNING".into(),
        }];
        a.sidebar.procs = vec![Proc {
            command: "npm run dev".into(),
            pid: "bg_1".into(),
        }];
        a
    }

    fn working() -> App {
        let mut a = conversation(TH);
        a.apply(PyMsg::SetWorking {
            working: true,
            message: "Thinking...".into(),
        });
        a.spinner = 3;
        a.top = 2;
        a.stick = false;
        a.set_input("also update the README".into());
        a
    }

    fn selector() -> App {
        let mut a = conversation(TH);
        a.apply(PyMsg::PromptSelect {
            title: "MODEL SELECTOR".into(),
            items: (0..40).map(|i| format!("model-{i}  [128k ctx]")).collect(),
        });
        a
    }

    #[test]
    fn snapshots() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("snapshots");
        std::fs::create_dir_all(&dir).unwrap();
        type Build = fn() -> App;
        let states: [(&str, Build, &[&str]); 4] = [
            (
                "empty",
                || {
                    let mut a = base_app(TH);
                    a.sidebar.tokens_used = 3_100;
                    a
                },
                &["▄", "┃", "Describe a task, or type / for commands."],
            ),
            (
                "conversation",
                || conversation(TH),
                &["▎", "nimcode", "y approve", "›"],
            ),
            ("working", working, &["╸", "Thinking", "more", "enter send"]),
            (
                "selector",
                selector,
                &["model selector", "model-0", "more", "enter choose"],
            ),
        ];
        for (name, build, glyphs) in states {
            for (w, h) in [(120u16, 36u16), (80, 24)] {
                let mut app = build();
                let s = snapshot(&mut app, w, h);
                for g in glyphs {
                    assert!(s.contains(g), "{name} {w}x{h} lacks {g:?}:\n{s}");
                }
                assert!(s.lines().all(|l| l.width() <= w as usize));
                if w == 120 && name != "empty" {
                    assert!(s.contains("41k of 128k"), "{name}: sidebar\n{s}");
                }
                if w == 80 && name != "empty" && name != "selector" {
                    assert!(s.contains("41k/128k"), "{name}: compact tokens\n{s}");
                }
                std::fs::write(dir.join(format!("{name}_{w}x{h}.txt")), &s).unwrap();
            }
        }
        // ASCII + NO_COLOR: only ASCII on screen, no colours in the buffer
        let plain_th = Theme {
            color: false,
            ascii: true,
        };
        let mut app = conversation(plain_th);
        let s = snapshot(&mut app, 100, 30);
        assert!(s.is_ascii(), "{s}");
        std::fs::write(dir.join("conversation_ascii_100x30.txt"), &s).unwrap();
        let mut term = Terminal::new(TestBackend::new(100, 30)).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        assert!(term.backend().buffer().content().iter().all(|c| {
            c.fg == ratatui::style::Color::Reset && c.bg == ratatui::style::Color::Reset
        }));
        // completion popup sits above the rule, typed text stays visible
        let mut app = conversation(TH);
        app.completions = vec!["/help".into(), "/history".into(), "/model".into()];
        app.set_input("/h".into());
        let s = snapshot(&mut app, 80, 24);
        assert!(
            s.contains("/help") && s.contains("/history") && !s.contains("/model"),
            "{s}"
        );
        assert!(s.contains("tab complete"), "{s}");
        std::fs::write(dir.join("popup_80x24.txt"), &s).unwrap();
        // tiny terminals never panic
        for (w, h) in [(1, 1), (19, 5), (40, 8), (89, 12)] {
            snapshot(&mut conversation(TH), w, h);
            snapshot(&mut base_app(TH), w, h);
            snapshot(&mut selector(), w, h);
        }
    }
}
