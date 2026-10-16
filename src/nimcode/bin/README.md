# Bundled nim-tui sidecar binary (not committed; gitignored).
#
# Release wheels are built per platform by .github/workflows/release.yml,
# which compiles tui-rs/ for the target and copies the binary here before
# `python -m build --wheel`, then retags the wheel for that platform.
#
# Local development needs nothing here: find_rust_binary() in
# ../ui/rust_bridge.py also finds tui-rs/target/release/ after
#   cargo build --release --manifest-path tui-rs/Cargo.toml
# or set NIM_TUI_BIN to a binary's path.
#
# To build a local wheel by hand, copy tui-rs/target/release/nim-tui[.exe]
# here first.
#
# Standalone check (no Python needed): `nim-tui[.exe] --demo`
