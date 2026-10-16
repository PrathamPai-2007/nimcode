//! "Nim" palette and glyph tables (colour / ASCII / NO_COLOR) in one place.
use ratatui::style::{Color, Modifier, Style};

pub const INK: Color = Color::Rgb(0x1A, 0x1D, 0x26);
pub const GRAPHITE: Color = Color::Rgb(0x2C, 0x31, 0x40);
pub const BONE: Color = Color::Rgb(0xE2, 0xDD, 0xD0);
pub const ASH: Color = Color::Rgb(0x8A, 0x8F, 0x9C);
pub const PINE: Color = Color::Rgb(0x6F, 0xA5, 0x8B);
pub const SULFUR: Color = Color::Rgb(0xE0, 0xB3, 0x54);
pub const WISTERIA: Color = Color::Rgb(0x9B, 0x8F, 0xD1);
pub const ERROR: Color = Color::Rgb(0xD0, 0x67, 0x5F);

pub struct Glyphs {
    pub gutter: &'static str,
    pub vrule: &'static str,
    pub hrule: &'static str,
    pub junction: &'static str,
    pub stick: &'static str,
    pub struck: &'static str,
    pub head: &'static str,
    pub fuse: &'static str,
    pub fuse_tip: &'static str,
    pub prompt: &'static str,
    pub ellipsis: &'static str,
    pub bullet: &'static str,
    pub done: &'static str,
    pub open: &'static str,
    pub up: &'static str,
    pub down: &'static str,
    pub sep: &'static str,
    pub quote: &'static str,
    pub cell: &'static str,
}

const UNICODE: Glyphs = Glyphs {
    gutter: "▎",
    vrule: "│",
    hrule: "─",
    junction: "┴",
    stick: "┃",
    struck: "╎",
    head: "▄",
    fuse: "━",
    fuse_tip: "╸",
    prompt: "›",
    ellipsis: "…",
    bullet: "•",
    done: "✓",
    open: "○",
    up: "↑",
    down: "↓",
    sep: "·",
    quote: "│",
    cell: "│",
};

const ASCII: Glyphs = Glyphs {
    gutter: "|",
    vrule: "|",
    hrule: "-",
    junction: "+",
    stick: "|",
    struck: ":",
    head: "o",
    fuse: "-",
    fuse_tip: "*",
    prompt: ">",
    ellipsis: "...",
    bullet: "-",
    done: "x",
    open: "o",
    up: "^",
    down: "v",
    sep: "-",
    quote: "|",
    cell: "|",
};

fn truthy(name: &str) -> bool {
    std::env::var(name)
        .map(|v| {
            matches!(
                v.trim().to_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

#[derive(Clone, Copy, Debug)]
pub struct Theme {
    pub color: bool,
    pub ascii: bool,
}

impl Theme {
    /// Same switches as nimcode.ui.output: NO_COLOR (any value), NIMCODE_NO_COLOR,
    /// NIMCODE_ASCII, NIMCODE_PLAIN (= both).
    pub fn from_env() -> Theme {
        let plain = truthy("NIMCODE_PLAIN");
        let no_color = std::env::var("NO_COLOR").is_ok_and(|v| !v.is_empty())
            || truthy("NIMCODE_NO_COLOR")
            || plain;
        Theme {
            color: !no_color,
            ascii: truthy("NIMCODE_ASCII") || plain,
        }
    }

    pub fn g(&self) -> &'static Glyphs {
        if self.ascii {
            &ASCII
        } else {
            &UNICODE
        }
    }

    /// Foreground only. Without colour, the quiet tones (Ash, Graphite) fall
    /// back to DIM so hierarchy survives; everything else is plain.
    pub fn fg(&self, c: Color) -> Style {
        if self.color {
            Style::default().fg(c)
        } else if c == ASH || c == GRAPHITE {
            Style::default().add_modifier(Modifier::DIM)
        } else {
            Style::default()
        }
    }

    /// The painted page: Bone on Ink (nothing at all under NO_COLOR).
    pub fn base(&self) -> Style {
        if self.color {
            Style::default().fg(BONE).bg(INK)
        } else {
            Style::default()
        }
    }

    /// Selected row: Graphite background, or reverse video without colour.
    pub fn selected(&self) -> Style {
        if self.color {
            Style::default().fg(BONE).bg(GRAPHITE)
        } else {
            Style::default().add_modifier(Modifier::REVERSED)
        }
    }

    pub fn mode_color(mode: &str) -> Color {
        match mode_word(mode) {
            "Plan" => WISTERIA,
            "YOLO" => SULFUR,
            _ => PINE,
        }
    }
}

/// "Build Mode" -> "Build".
pub fn mode_word(mode: &str) -> &str {
    let m = mode.trim();
    m.strip_suffix(" Mode").unwrap_or(m)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ascii_table_is_ascii_and_modes_map() {
        let g = Theme {
            color: false,
            ascii: true,
        }
        .g();
        for s in [
            g.gutter, g.vrule, g.hrule, g.junction, g.stick, g.struck, g.head, g.fuse, g.fuse_tip,
            g.prompt, g.ellipsis, g.bullet, g.done, g.open, g.up, g.down, g.sep, g.quote, g.cell,
        ] {
            assert!(s.is_ascii(), "{s}");
        }
        assert_eq!(mode_word("YOLO Mode"), "YOLO");
        assert_eq!(Theme::mode_color("Plan Mode"), WISTERIA);
        assert_eq!(Theme::mode_color("Build Mode"), PINE);
        let nc = Theme {
            color: false,
            ascii: false,
        };
        assert_eq!(nc.fg(PINE), Style::default());
        assert_eq!(nc.base(), Style::default());
    }
}
