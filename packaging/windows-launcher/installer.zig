const std = @import("std");
const builtin = @import("builtin");

const LAUNCHER_VERSION = "1.0.0";

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    std.debug.print(
        \\Paperclip Installer {s}
        \\====================
        \\
        \\This installer will set up Paperclip to run with a double-click.
        \\
        \\Requirements:
        \\  - Windows 10/11 x64
        \\  - A distribution beside this launcher containing runtime\node
        \\
        \\The installer will:
        \\  1. Install Paperclip to %LOCALAPPDATA%\Paperclip
        \\  2. Copy the bundled Node.js runtime with the app
        \\  3. Create a double-click launcher
        \\  4. Run 'paperclip doctor' to verify
        \\
        \\
    , .{LAUNCHER_VERSION});

    // Determine install directory
    var install_dir: []u8 = undefined;
    if (builtin.os.tag == .windows) {
        const local_app_data = std.process.getEnvVarOwned(allocator, "LOCALAPPDATA") catch try allocator.dupe(u8, ".");
        defer allocator.free(local_app_data);
        install_dir = try std.fs.path.join(allocator, &[_][]const u8{ local_app_data, "Paperclip" });
    } else {
        const home = std.process.getEnvVarOwned(allocator, "HOME") catch try allocator.dupe(u8, ".");
        defer allocator.free(home);
        install_dir = try std.fs.path.join(allocator, &[_][]const u8{ home, ".local", "share", "paperclip" });
    }
    defer allocator.free(install_dir);

    std.debug.print("\nInstall directory: {s}\n", .{install_dir});

    // Create install dir
    std.fs.cwd().makePath(install_dir) catch |err| {
        std.debug.print("Failed to create install dir: {any}\n", .{err});
        std.process.exit(1);
    };

    // Find payload source: beside installer, or in build/payload
    var payload_src: ?[]u8 = null;
    defer if (payload_src) |p| allocator.free(p);
    var runtime_src: ?[]u8 = null;
    defer if (runtime_src) |p| allocator.free(p);

    const exe_dir = std.fs.selfExeDirPathAlloc(allocator) catch null;
    if (exe_dir) |dir| {
        defer allocator.free(dir);
        const candidates = [_][]const u8{
            try std.fs.path.join(allocator, &[_][]const u8{ dir, "payload" }),
            try std.fs.path.join(allocator, &[_][]const u8{ dir, "..", "build", "payload" }),
            try std.fs.path.join(allocator, &[_][]const u8{ ".", "packaging", "exe", "build", "payload" }),
        };
        for (candidates) |cand| {
            defer allocator.free(cand);
            if (std.fs.cwd().statFile(cand) catch null) |s| {
                if (s.kind == .directory) {
                    payload_src = try allocator.dupe(u8, cand);
                    break;
                }
            }
        }

        const runtime_candidates = [_][]const u8{
            try std.fs.path.join(allocator, &[_][]const u8{ dir, "runtime" }),
            try std.fs.path.join(allocator, &[_][]const u8{ dir, "..", "build", "runtime" }),
            try std.fs.path.join(allocator, &[_][]const u8{ ".", "packaging", "exe", "build", "runtime" }),
        };
        const runtime_node_rel = if (builtin.os.tag == .windows) "node/node.exe" else "node/bin/node";
        for (runtime_candidates) |candidate| {
            defer allocator.free(candidate);
            const node_path = try std.fs.path.join(allocator, &[_][]const u8{ candidate, runtime_node_rel });
            defer allocator.free(node_path);
            if (std.fs.cwd().statFile(node_path) catch null) |s| {
                if (s.kind == .file) {
                    runtime_src = try allocator.dupe(u8, candidate);
                    break;
                }
            }
        }
    }

    if (payload_src == null) {
        std.debug.print("\nPayload not found beside installer.\n", .{});
        std.debug.print("For development, run: python3 packaging/exe/build_exe.py --stage-payload --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze\n", .{});
        std.debug.print("Then re-run installer.\n", .{});
        std.debug.print("\nAlternatively, this installer can set up a minimal install that uses\n", .{});
        std.debug.print("the repository's cli/dist and node_modules via NODE_PATH.\n", .{});
        // Create minimal launcher that points to repo
        const repo_root = if (exe_dir) |d| try std.fs.path.join(allocator, &[_][]const u8{ d, "..", ".." }) else try allocator.dupe(u8, ".");
        defer allocator.free(repo_root);
        std.debug.print("Using repo root: {s}\n", .{repo_root});
        // Copy Zig launcher to install dir
        const launcher_src = try std.fs.path.join(allocator, &[_][]const u8{ exe_dir orelse ".", if (builtin.os.tag == .windows) "paperclip.exe" else "paperclip" });
        defer allocator.free(launcher_src);
        const launcher_dst = try std.fs.path.join(allocator, &[_][]const u8{ install_dir, if (builtin.os.tag == .windows) "paperclip.exe" else "paperclip" });
        defer allocator.free(launcher_dst);
        // If launcher exists beside installer, copy it
        if (std.fs.cwd().statFile(launcher_src) catch null) |_| {
            try std.fs.cwd().copyFile(launcher_src, std.fs.cwd(), launcher_dst, .{});
            std.debug.print("Copied launcher to {s}\n", .{launcher_dst});
        } else {
            // Copy self as launcher
            const self_path = try std.fs.selfExePathAlloc(allocator);
            defer allocator.free(self_path);
            // Self is installer, not launcher, so we need launcher binary
            std.debug.print("Launcher binary not found, install may be incomplete.\n", .{});
        }
        std.debug.print("\nInstall completed (minimal). Run {s} doctor to verify.\n", .{install_dir});
        return;
    }

    if (runtime_src == null) {
        std.debug.print("\nBundled Node runtime not found beside installer.\n", .{});
        std.debug.print("Rebuild with --embed-node, then distribute the runtime folder with this installer.\n", .{});
        std.process.exit(1);
    }

    std.debug.print("Found payload at: {s}\n", .{payload_src.?});
    std.debug.print("Found bundled runtime at: {s}\n", .{runtime_src.?});

    // Copy payload to install dir
    std.debug.print("Copying payload (this may take a minute)...\n", .{});
    const payload_dst = try std.fs.path.join(allocator, &[_][]const u8{ install_dir, "payload" });
    defer allocator.free(payload_dst);

    // Remove existing payload
    std.fs.cwd().deleteTree(payload_dst) catch {};

    // Copy recursively - use std.fs
    try copyDir(allocator, payload_src.?, payload_dst);

    const runtime_dst = try std.fs.path.join(allocator, &[_][]const u8{ install_dir, "runtime" });
    defer allocator.free(runtime_dst);
    std.fs.cwd().deleteTree(runtime_dst) catch {};
    try copyDir(allocator, runtime_src.?, runtime_dst);

    std.debug.print("Payload and bundled runtime copied.\n", .{});

    // Copy launcher exe to install dir
    const launcher_name = if (builtin.os.tag == .windows) "paperclip.exe" else "paperclip";
    const launcher_src_candidates = [_][]const u8{
        try std.fs.path.join(allocator, &[_][]const u8{ exe_dir orelse ".", launcher_name }),
        try std.fs.path.join(allocator, &[_][]const u8{ exe_dir orelse ".", "..", "windows-launcher", launcher_name }),
        try std.fs.path.join(allocator, &[_][]const u8{ ".", "packaging", "windows-launcher", launcher_name }),
    };
    var launcher_copied = false;
    for (launcher_src_candidates) |cand| {
        defer allocator.free(cand);
        if (std.fs.cwd().statFile(cand) catch null) |s| {
            if (s.kind == .file) {
                const dst = try std.fs.path.join(allocator, &[_][]const u8{ install_dir, launcher_name });
                defer allocator.free(dst);
                try std.fs.cwd().copyFile(cand, std.fs.cwd(), dst, .{});
                std.debug.print("Copied launcher to {s}\n", .{dst});
                launcher_copied = true;
                break;
            }
        }
    }
    if (!launcher_copied) {
        std.debug.print("Warning: launcher exe not found, using installer as launcher (not ideal)\n", .{});
        const self_path = try std.fs.selfExePathAlloc(allocator);
        defer allocator.free(self_path);
        const dst = try std.fs.path.join(allocator, &[_][]const u8{ install_dir, launcher_name });
        defer allocator.free(dst);
        std.fs.cwd().copyFile(self_path, std.fs.cwd(), dst, .{}) catch {};
    }

    // Create batch file wrapper for double-click on Windows
    if (builtin.os.tag == .windows) {
        const bat_path = try std.fs.path.join(allocator, &[_][]const u8{ install_dir, "paperclip.bat" });
        defer allocator.free(bat_path);
        const bat_content = "@echo off\r\n\"%~dp0paperclip.exe\" %*\r\n";
        const bat_file = try std.fs.cwd().createFile(bat_path, .{});
        defer bat_file.close();
        try bat_file.writeAll(bat_content);
        std.debug.print("Created batch wrapper: {s}\n", .{bat_path});
    }

    std.debug.print("\nInstall completed!\n", .{});
    std.debug.print("You can now:\n", .{});
    std.debug.print("  - Double-click {s}{s}{s} to run doctor\n", .{ install_dir, std.fs.path.sep_str, launcher_name });
    std.debug.print("  - Add {s} to your PATH\n", .{install_dir});
    std.debug.print("  - Run: paperclip onboard\n", .{});

    // Run doctor
    std.debug.print("\nRunning paperclip doctor...\n", .{});
    const launcher_path = try std.fs.path.join(allocator, &[_][]const u8{ install_dir, launcher_name });
    defer allocator.free(launcher_path);
    var child = std.process.Child.init(&[_][]const u8{ launcher_path, "doctor" }, allocator);
    child.stdin_behavior = .Inherit;
    child.stdout_behavior = .Inherit;
    child.stderr_behavior = .Inherit;
    _ = child.spawnAndWait() catch {};
}

fn copyDir(allocator: std.mem.Allocator, src: []const u8, dst: []const u8) !void {
    try std.fs.cwd().makePath(dst);
    var dir = try std.fs.cwd().openIterableDir(src, .{});
    defer dir.close();
    var iter = dir.iterate();
    while (try iter.next()) |entry| {
        const src_path = try std.fs.path.join(allocator, &[_][]const u8{ src, entry.name });
        defer allocator.free(src_path);
        const dst_path = try std.fs.path.join(allocator, &[_][]const u8{ dst, entry.name });
        defer allocator.free(dst_path);
        switch (entry.kind) {
            .directory => {
                try copyDir(allocator, src_path, dst_path);
            },
            .file => {
                try std.fs.cwd().copyFile(src_path, std.fs.cwd(), dst_path, .{});
            },
            .sym_link => {
                // Resolve symlink and copy as real file/dir
                const real = std.fs.cwd().realpathAlloc(allocator, src_path) catch continue;
                defer allocator.free(real);
                if (std.fs.cwd().statFile(real) catch null) |s| {
                    if (s.kind == .directory) {
                        try copyDir(allocator, real, dst_path);
                    } else {
                        try std.fs.cwd().copyFile(real, std.fs.cwd(), dst_path, .{});
                    }
                }
            },
            else => {},
        }
    }
}
