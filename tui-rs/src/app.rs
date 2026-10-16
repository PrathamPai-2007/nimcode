//! UI state, input editing and key handling — pure (no I/O) so tests can drive it.
use crate::theme::Theme;
use crate::{clean_text, PyMsg, RustMsg, Sidebar};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseEvent, MouseEventKind};
use ratatui::text::Line;
use std::collections::VecDeque;
use std::time::{Duration, Instant};

pub const MAX_MESSAGES: usize = 5000;
pub const MAX_HISTORY: usize = 200;
const MODES: [&str; 3] = ["Plan Mode", "Build Mode", "YOLO Mode"];

/// Rendered lines of one message, valid for (width, folded).
pub struct Cache {
    pub width: usize,
    pub folded: bool,
    pub lines: Vec<Line<'static>>,
}

pub struct Msg {
    pub role: String,
    pub content: String,
    /// Speaker name on the first line (only when the speaker changes; tools always).
    pub label: bool,
    /// Blank line above (consecutive system notes stay tight).
    pub gap: bool,
    pub cache: Option<Cache>,
}

pub struct Selector {
    pub title: String,
    pub items: Vec<String>,
    pub idx: usize,
    pub top: usize,
}

pub struct App {
    pub theme: Theme,
    pub msgs: VecDeque<Msg>,
    pub sidebar: Sidebar,
    pub input: String,
    /// Byte offset into `input`, always on a char boundary.
    pub cursor: usize,
    pub history: Vec<String>,
    pub hist_idx: Option<usize>,
    pub draft: String,
    pub completions: Vec<String>,
    pub comp_idx: usize,
    pub comp_dismissed_for: Option<String>,
    /// First visible conversation row; `stick` keeps it pinned to the bottom.
    pub top: usize,
    pub stick: bool,
    pub selector: Option<Selector>,
    pub fold: bool,
    pub spinner: usize,
    pub working_since: Option<Instant>,
    pub flash_until: Option<Instant>,
    pub shift_enter: bool,
    // Written by the last draw, read by scrolling.
    pub view_h: usize,
    pub max_top: usize,
    pub below: usize,
    pub sel_rows: usize,
}

impl App {
    pub fn new(theme: Theme) -> App {
        App {
            theme,
            msgs: VecDeque::new(),
            sidebar: Sidebar::default(),
            input: String::new(),
            cursor: 0,
            history: vec![],
            hist_idx: None,
            draft: String::new(),
            completions: vec![],
            comp_idx: 0,
            comp_dismissed_for: None,
            top: 0,
            stick: true,
            selector: None,
            fold: true,
            spinner: 0,
            working_since: None,
            flash_until: None,
            shift_enter: cfg!(windows),
            view_h: 10,
            max_top: 0,
            below: 0,
            sel_rows: 10,
        }
    }

    pub fn push(&mut self, role: &str, content: &str) {
        let role = role.to_string();
        let prev = self.msgs.back().map(|m| m.role.as_str());
        let prev_speaker = self
            .msgs
            .iter()
            .rev()
            .find(|m| m.role != "system")
            .map(|m| m.role.as_str());
        let label = role == "tool" || (role != "system" && prev_speaker != Some(role.as_str()));
        let gap = prev.is_some() && !(role == "system" && prev == Some("system"));
        self.msgs.push_back(Msg {
            role,
            content: clean_text(content),
            label,
            gap,
            cache: None,
        });
        if self.msgs.len() > MAX_MESSAGES {
            self.msgs.pop_front();
            if let Some(m) = self.msgs.front_mut() {
                m.gap = false;
            }
        }
    }

    /// Only system notes so far (the welcome line): show the empty-state board.
    pub fn is_empty_state(&self) -> bool {
        self.msgs.iter().all(|m| m.role == "system")
    }

    /// Apply a bridge message; true means quit.
    pub fn apply(&mut self, m: PyMsg) -> bool {
        match m {
            PyMsg::AddMessage { role, content } => self.push(&role, &content),
            PyMsg::System { content } => self.push("system", &content),
            // set_working is authoritative for the spinner.
            PyMsg::Update { sidebar: mut s } => {
                s.is_working = self.sidebar.is_working;
                s.working_message = std::mem::take(&mut self.sidebar.working_message);
                self.sidebar = s;
            }
            PyMsg::SetWorking { working, message } => {
                if working && !self.sidebar.is_working {
                    self.working_since = Some(Instant::now());
                    self.spinner = 0;
                }
                if !working && self.sidebar.is_working {
                    // the one completion flash: final message's gutter in Sulfur
                    self.flash_until = Some(Instant::now() + Duration::from_millis(300));
                    self.working_since = None;
                }
                self.sidebar.is_working = working;
                self.sidebar.working_message = message;
            }
            PyMsg::Clear => {
                self.msgs.clear();
                self.top = 0;
                self.stick = true;
            }
            PyMsg::PromptSelect { title, items } => {
                self.selector = Some(Selector {
                    title,
                    items,
                    idx: 0,
                    top: 0,
                })
            }
            PyMsg::SetCompletions { items } => {
                self.completions = items;
                self.comp_idx = 0;
                self.comp_dismissed_for = None;
            }
            PyMsg::Quit => return true,
        }
        false
    }

    pub fn flashing(&self) -> bool {
        self.flash_until.is_some_and(|t| Instant::now() < t)
    }

    /// Wake-up interval for the event loop: animate only while needed.
    pub fn tick(&self) -> Duration {
        if self.sidebar.is_working || self.flashing() {
            Duration::from_millis(120)
        } else {
            Duration::from_secs(1)
        }
    }

    // ---------- scrolling ----------

    pub fn scroll_up(&mut self, n: usize) {
        let from = if self.stick { self.max_top } else { self.top };
        self.top = from.saturating_sub(n);
        self.stick = false;
    }

    pub fn scroll_down(&mut self, n: usize) {
        if self.stick {
            return;
        }
        self.top = self.top.saturating_add(n);
        if self.top >= self.max_top {
            self.stick = true;
        }
    }

    // ---------- completion popup ----------

    pub fn popup(&self) -> Vec<usize> {
        if self.selector.is_some()
            || self.comp_dismissed_for.as_deref() == Some(self.input.as_str())
        {
            return vec![];
        }
        popup_matches(&self.completions, &self.input)
    }

    // ---------- input editing ----------

    fn edited(&mut self) {
        self.hist_idx = None;
        self.comp_dismissed_for = None;
        self.comp_idx = 0;
    }

    pub fn set_input(&mut self, s: String) {
        self.cursor = s.len();
        self.input = s;
    }

    pub fn insert(&mut self, s: &str) {
        self.input.insert_str(self.cursor, s);
        self.cursor += s.len();
        self.edited();
    }

    fn line_start(&self) -> usize {
        self.input[..self.cursor].rfind('\n').map_or(0, |i| i + 1)
    }

    fn line_end(&self) -> usize {
        self.input[self.cursor..]
            .find('\n')
            .map_or(self.input.len(), |i| self.cursor + i)
    }

    fn prev_char(&self, at: usize) -> usize {
        self.input[..at]
            .char_indices()
            .next_back()
            .map_or(0, |(i, _)| i)
    }

    fn next_char(&self, at: usize) -> usize {
        self.input[at..]
            .chars()
            .next()
            .map_or(at, |c| at + c.len_utf8())
    }

    fn word_left(&self) -> usize {
        let s = &self.input[..self.cursor];
        let t = s.trim_end_matches(char::is_whitespace);
        t.rfind(char::is_whitespace)
            .map_or(0, |i| i + t[i..].chars().next().unwrap().len_utf8())
    }

    fn word_right(&self) -> usize {
        let s = &self.input[self.cursor..];
        let lead = s.len() - s.trim_start_matches(char::is_whitespace).len();
        let rest = &s[lead..];
        self.cursor + lead + rest.find(char::is_whitespace).unwrap_or(rest.len())
    }

    fn delete_range(&mut self, a: usize, b: usize) {
        if a < b {
            self.input.replace_range(a..b, "");
            self.cursor = a;
            self.edited();
        }
    }

    /// Move to the previous/next logical line at the same char column.
    fn move_line(&mut self, up: bool) {
        let ls = self.line_start();
        let col = self.input[ls..self.cursor].chars().count();
        let target = if up {
            if ls == 0 {
                return;
            }
            self.input[..ls - 1].rfind('\n').map_or(0, |i| i + 1)
        } else {
            let le = self.line_end();
            if le == self.input.len() {
                return;
            }
            le + 1
        };
        let line = &self.input[target..];
        let line = &line[..line.find('\n').unwrap_or(line.len())];
        let off: usize = line.chars().take(col).map(char::len_utf8).sum();
        self.cursor = target + off;
    }

    fn history_prev(&mut self) {
        if self.history.is_empty() {
            return;
        }
        let i = match self.hist_idx {
            None => {
                self.draft = self.input.clone();
                self.history.len() - 1
            }
            Some(i) => i.saturating_sub(1),
        };
        self.hist_idx = Some(i);
        self.set_input(self.history[i].clone());
    }

    fn history_next(&mut self) {
        let Some(i) = self.hist_idx else { return };
        if i + 1 < self.history.len() {
            self.hist_idx = Some(i + 1);
            self.set_input(self.history[i + 1].clone());
        } else {
            self.hist_idx = None;
            let d = std::mem::take(&mut self.draft);
            self.set_input(d);
        }
    }

    fn submit(&mut self) -> RustMsg {
        let text = std::mem::take(&mut self.input);
        self.cursor = 0;
        if !text.trim().is_empty() && self.history.last() != Some(&text) {
            self.history.push(text.clone());
            if self.history.len() > MAX_HISTORY {
                self.history.remove(0);
            }
        }
        self.edited();
        self.draft.clear();
        self.stick = true;
        RustMsg::Input { text }
    }

    pub fn on_paste(&mut self, s: &str) {
        if self.selector.is_none() {
            let t = clean_text(&s.replace("\r\n", "\n").replace('\r', "\n"));
            self.insert(&t);
        }
    }

    pub fn on_mouse(&mut self, m: MouseEvent) -> bool {
        let up = match m.kind {
            MouseEventKind::ScrollUp => true,
            MouseEventKind::ScrollDown => false,
            _ => return false,
        };
        if let Some(sel) = self.selector.as_mut() {
            sel.idx = if up {
                sel.idx.saturating_sub(1)
            } else {
                (sel.idx + 1).min(sel.items.len().saturating_sub(1))
            };
            sel.top = selector_follow(sel.top, sel.idx, self.sel_rows, sel.items.len());
        } else if up {
            self.scroll_up(3);
        } else {
            self.scroll_down(3);
        }
        true
    }

    /// Handle a key press. `burst` = more keys already queued behind this one
    /// (a paste on terminals without bracketed paste, e.g. Windows consoles),
    /// so a plain Enter there inserts a newline instead of sending.
    pub fn on_key(&mut self, k: KeyEvent, burst: bool) -> Vec<RustMsg> {
        let ctrl = k.modifiers.contains(KeyModifiers::CONTROL);
        let alt = k.modifiers.contains(KeyModifiers::ALT);
        let shift = k.modifiers.contains(KeyModifiers::SHIFT);
        if self.selector.is_some() {
            return self.selector_key(k, ctrl);
        }
        let matches = self.popup();
        let popup_open = !matches.is_empty();
        if popup_open && self.comp_idx >= matches.len() {
            self.comp_idx = 0;
        }
        let sel = matches.get(self.comp_idx).copied();
        let mut out = vec![];
        match k.code {
            KeyCode::Tab if popup_open => {
                if let Some(ci) = sel {
                    let s = accept_completion(&self.input, &self.completions[ci]);
                    self.set_input(s);
                    self.comp_dismissed_for = None;
                }
            }
            KeyCode::Up if popup_open => self.comp_idx = self.comp_idx.saturating_sub(1),
            KeyCode::Down if popup_open => {
                self.comp_idx = (self.comp_idx + 1).min(matches.len() - 1)
            }
            KeyCode::Esc if popup_open => self.comp_dismissed_for = Some(self.input.clone()),
            KeyCode::Enter if shift || alt || ctrl || burst => self.insert("\n"),
            KeyCode::Char('j') if ctrl => self.insert("\n"),
            KeyCode::Enter => {
                if let Some(ci) = sel {
                    let s = accept_completion(&self.input, &self.completions[ci]);
                    self.set_input(s);
                }
                out.push(self.submit());
            }
            KeyCode::Char('c') if ctrl => {
                if self.input.is_empty() {
                    out.push(RustMsg::Interrupt);
                } else {
                    self.set_input(String::new());
                    self.edited();
                }
            }
            KeyCode::Char('d') if ctrl => {
                if self.input.is_empty() {
                    out.push(RustMsg::Eof);
                } else {
                    let n = self.next_char(self.cursor);
                    self.delete_range(self.cursor, n);
                }
            }
            KeyCode::Char('o') if ctrl => {
                self.fold = !self.fold;
            }
            KeyCode::Char('u') if ctrl => self.delete_range(self.line_start(), self.cursor),
            KeyCode::Char('w') if ctrl => self.delete_range(self.word_left(), self.cursor),
            KeyCode::Char('h') if ctrl => self.delete_range(self.word_left(), self.cursor),
            KeyCode::BackTab => out.push(self.cycle_mode()),
            KeyCode::Char('t') if ctrl => out.push(self.cycle_mode()),
            KeyCode::Backspace if ctrl || alt => self.delete_range(self.word_left(), self.cursor),
            KeyCode::Backspace => self.delete_range(self.prev_char(self.cursor), self.cursor),
            KeyCode::Delete => {
                let n = self.next_char(self.cursor);
                self.delete_range(self.cursor, n);
            }
            KeyCode::Left if ctrl => self.cursor = self.word_left(),
            KeyCode::Right if ctrl => self.cursor = self.word_right(),
            KeyCode::Left => self.cursor = self.prev_char(self.cursor),
            KeyCode::Right => self.cursor = self.next_char(self.cursor),
            KeyCode::Home => self.cursor = self.line_start(),
            KeyCode::End => self.cursor = self.line_end(),
            KeyCode::Up if ctrl => self.scroll_up(1),
            KeyCode::Down if ctrl => self.scroll_down(1),
            KeyCode::Up if self.input.contains('\n') => self.move_line(true),
            KeyCode::Down if self.input.contains('\n') => self.move_line(false),
            KeyCode::Up => self.history_prev(),
            KeyCode::Down => self.history_next(),
            KeyCode::PageUp => self.scroll_up(self.view_h.saturating_sub(2).max(1)),
            KeyCode::PageDown => self.scroll_down(self.view_h.saturating_sub(2).max(1)),
            KeyCode::Esc => self.stick = true,
            // Ctrl+letter is a shortcut, not text (Ctrl+Alt = AltGr on Windows)
            KeyCode::Char(_) if ctrl && !alt => {}
            KeyCode::Char(c) => {
                let mut b = [0u8; 4];
                self.insert(c.encode_utf8(&mut b));
            }
            _ => {}
        }
        out
    }

    fn cycle_mode(&mut self) -> RustMsg {
        // cycle from the mode Python last reported
        let cur = MODES
            .iter()
            .position(|m| *m == self.sidebar.mode)
            .unwrap_or(1);
        let next = MODES[(cur + 1) % MODES.len()];
        self.sidebar.mode = next.into();
        RustMsg::ModeCycle { mode: next.into() }
    }

    fn selector_key(&mut self, k: KeyEvent, ctrl: bool) -> Vec<RustMsg> {
        let rows = self.sel_rows;
        let sel = self.selector.as_mut().unwrap();
        let last = sel.items.len().saturating_sub(1);
        let page = rows.saturating_sub(1).max(1);
        let done = |s: &mut Self, index| {
            s.selector = None;
            vec![RustMsg::SelectResult { index }]
        };
        match k.code {
            KeyCode::Up => sel.idx = sel.idx.saturating_sub(1),
            KeyCode::Down => sel.idx = (sel.idx + 1).min(last),
            KeyCode::PageUp => sel.idx = sel.idx.saturating_sub(page),
            KeyCode::PageDown => sel.idx = (sel.idx + page).min(last),
            KeyCode::Home => sel.idx = 0,
            KeyCode::End => sel.idx = last,
            KeyCode::Enter if !sel.items.is_empty() => {
                let i = sel.idx;
                return done(self, Some(i));
            }
            // empty list: nothing to pick
            KeyCode::Enter | KeyCode::Esc => return done(self, None),
            KeyCode::Char('c') if ctrl => return done(self, None),
            _ => return vec![],
        }
        sel.top = selector_follow(sel.top, sel.idx, rows, sel.items.len());
        vec![]
    }
}

// Slash-command autocomplete helpers (pure; unit-tested below).

/// The word being completed, if the input is a bare `/command` fragment.
pub fn completion_word(input: &str) -> Option<&str> {
    let word = input.strip_prefix('/')?;
    if word.contains(char::is_whitespace) {
        return None;
    }
    Some(word)
}

/// Catalog indices whose entry matches the word (prefix, case-insensitive).
pub fn filter_completions(items: &[String], word: &str) -> Vec<usize> {
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

pub fn popup_matches(completions: &[String], input: &str) -> Vec<usize> {
    match completion_word(input) {
        Some(w) => filter_completions(completions, w),
        None => vec![],
    }
}

/// Replace the command word with the full entry, keeping typed args.
pub fn accept_completion(input: &str, entry: &str) -> String {
    match input.find(char::is_whitespace) {
        Some(i) => format!("{entry}{}", &input[i..]),
        None => entry.to_string(),
    }
}

/// Keep `idx` visible: move the window `top` just enough, clamped to range.
pub fn selector_follow(top: usize, idx: usize, visible: usize, total: usize) -> usize {
    let visible = visible.max(1);
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
    fn key(code: KeyCode, m: KeyModifiers) -> KeyEvent {
        KeyEvent::new(code, m)
    }
    fn typ(app: &mut App, s: &str) {
        for c in s.chars() {
            app.on_key(key(KeyCode::Char(c), KeyModifiers::NONE), false);
        }
    }
    fn app() -> App {
        App::new(Theme {
            color: true,
            ascii: false,
        })
    }
    const N: KeyModifiers = KeyModifiers::NONE;
    const C: KeyModifiers = KeyModifiers::CONTROL;

    #[test]
    fn cursor_editing() {
        let mut a = app();
        typ(&mut a, "hello wörld");
        a.on_key(key(KeyCode::Left, C), false);
        assert_eq!(&a.input[a.cursor..], "wörld");
        a.on_key(key(KeyCode::Backspace, N), false);
        assert_eq!(a.input, "hellowörld");
        a.on_key(key(KeyCode::Home, N), false);
        a.on_key(key(KeyCode::Delete, N), false);
        assert_eq!(a.input, "ellowörld");
        a.on_key(key(KeyCode::End, N), false);
        a.on_key(key(KeyCode::Char('w'), C), false);
        assert_eq!(a.input, "");
        typ(&mut a, "one two");
        a.on_key(key(KeyCode::Left, N), false);
        a.on_key(key(KeyCode::Right, C), false);
        assert_eq!(a.cursor, a.input.len());
        a.on_key(key(KeyCode::Char('u'), C), false);
        assert_eq!(a.input, "");
    }

    #[test]
    fn multiline_and_burst_enter_never_send() {
        let mut a = app();
        typ(&mut a, "ab");
        assert!(a
            .on_key(key(KeyCode::Enter, KeyModifiers::SHIFT), false)
            .is_empty());
        assert!(a.on_key(key(KeyCode::Char('j'), C), false).is_empty());
        assert!(a.on_key(key(KeyCode::Enter, N), true).is_empty());
        typ(&mut a, "cd");
        assert_eq!(a.input, "ab\n\n\ncd");
        // up/down move between lines, keep the column
        a.on_key(key(KeyCode::Up, N), false);
        a.on_key(key(KeyCode::Up, N), false);
        a.on_key(key(KeyCode::Up, N), false);
        assert_eq!(a.cursor, 0);
        a.on_key(key(KeyCode::End, N), false);
        a.on_paste("x\r\ny");
        assert_eq!(a.input, "abx\ny\n\n\ncd");
        let out = a.on_key(key(KeyCode::Enter, N), false);
        assert!(matches!(&out[..], [RustMsg::Input { text }] if text == "abx\ny\n\n\ncd"));
    }

    #[test]
    fn history_recall_dedup_and_cap() {
        let mut a = app();
        for s in ["one", "two", "two"] {
            typ(&mut a, s);
            a.on_key(key(KeyCode::Enter, N), false);
        }
        assert_eq!(a.history, vec!["one", "two"]);
        typ(&mut a, "dra");
        a.on_key(key(KeyCode::Up, N), false);
        assert_eq!(a.input, "two");
        a.on_key(key(KeyCode::Up, N), false);
        a.on_key(key(KeyCode::Up, N), false);
        assert_eq!(a.input, "one");
        a.on_key(key(KeyCode::Down, N), false);
        a.on_key(key(KeyCode::Down, N), false);
        assert_eq!(a.input, "dra");
        for i in 0..300 {
            a.set_input(format!("m{i}"));
            a.on_key(key(KeyCode::Enter, N), false);
        }
        assert_eq!(a.history.len(), MAX_HISTORY);
        assert_eq!(a.history[0], "m100");
    }

    #[test]
    fn scroll_and_fold_and_ctrl_keys() {
        let mut a = app();
        a.max_top = 50;
        a.view_h = 12;
        a.on_key(key(KeyCode::PageUp, N), false);
        assert!(!a.stick);
        assert_eq!(a.top, 40);
        a.on_key(key(KeyCode::Esc, N), false);
        assert!(a.stick);
        a.on_key(key(KeyCode::Char('o'), C), false);
        assert!(!a.fold);
        assert!(matches!(
            &a.on_key(key(KeyCode::Char('c'), C), false)[..],
            [RustMsg::Interrupt]
        ));
        assert!(matches!(
            &a.on_key(key(KeyCode::BackTab, KeyModifiers::SHIFT), false)[..],
            [RustMsg::ModeCycle { mode }] if mode == "YOLO Mode"
        ));
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
        assert_eq!(filter_completions(&items, ""), vec![0, 1, 2, 3, 4]);
        assert_eq!(filter_completions(&items, "he"), vec![0]);
        assert_eq!(filter_completions(&items, "TE"), vec![3, 4]);
        assert_eq!(filter_completions(&items, "zzz"), Vec::<usize>::new());
        assert_eq!(popup_matches(&items, "/t"), vec![3, 4]);
        assert_eq!(popup_matches(&items, "plain"), Vec::<usize>::new());
        assert_eq!(accept_completion("/he", "/help"), "/help");
        assert_eq!(accept_completion("/he x", "/help"), "/help x");
        // Enter on an open popup sends the highlighted command
        let mut a = app();
        a.completions = items;
        typ(&mut a, "/he");
        let out = a.on_key(key(KeyCode::Enter, N), false);
        assert!(matches!(&out[..], [RustMsg::Input { text }] if text == "/help"));
    }

    #[test]
    fn selector_window_follows_and_clamps() {
        assert_eq!(selector_follow(0, 3, 18, 5), 0);
        assert_eq!(selector_follow(0, 18, 18, 67), 1);
        assert_eq!(selector_follow(1, 66, 18, 67), 49);
        assert_eq!(selector_follow(49, 10, 18, 67), 10);
        assert_eq!(selector_follow(5, 10, 18, 67), 5);
        let mut a = app();
        a.apply(PyMsg::PromptSelect {
            title: "t".into(),
            items: (0..30).map(|i| i.to_string()).collect(),
        });
        a.sel_rows = 5;
        for _ in 0..7 {
            a.on_key(key(KeyCode::Down, N), false);
        }
        let s = a.selector.as_ref().unwrap();
        assert_eq!((s.idx, s.top), (7, 3));
        let out = a.on_key(key(KeyCode::Enter, N), false);
        assert!(matches!(
            &out[..],
            [RustMsg::SelectResult { index: Some(7) }]
        ));
        assert!(a.selector.is_none());
    }
}
