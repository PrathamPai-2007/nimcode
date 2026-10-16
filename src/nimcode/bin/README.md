# Bundled nim-tui sidecar binaries (one per platform).
#
# Refresh after changing tui-rs/:
#   cargo build --release --manifest-path tui-rs/Cargo.toml
#   cp tui-rs/target/release/nim-tui[.exe] src/nimcode/bin/
#
# find_rust_binary() in ../rust_bridge.py picks the bundled binary first,
# so `pip install .` just works with no extra setup. Linux/macOS contributors:
# add your `nim-tui` build here alongside the Windows .exe.
#
# Standalone check (no Python needed): `nim-tui[.exe] --demo`
