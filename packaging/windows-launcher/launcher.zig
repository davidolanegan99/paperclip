const std = @import("std");
const builtin = @import("builtin");

const MINIMUM_NODE_VERSION = "24.11.0";
const LAUNCHER_VERSION = "1.0.0";

const LAUNCHER_FLAGS_WITH_VALUE = [_][]const u8{
    "--launcher-node",
    "--launcher-payload",
};

fn isLauncherFlag(arg: []const u8) bool {
    return std.mem.startsWith(u8, arg, "--launcher-");
}

fn flagValue(flags: []const []const u8, name: []const u8) ?[]const u8 {
    for (flags, 0..) |flag, idx| {
        if (std.mem.startsWith(u8, flag, name)) {
            if (flag.len > name.len and flag[name.len] == '=') {
                return flag[name.len + 1 ..];
            }
            if (std.mem.eql(u8, flag, name) and idx + 1 < flags.len) {
                return flags[idx + 1];
            }
        }
    }
    return null;
}

fn hasFlag(flags: []const []const u8, name: []const u8) bool {
    for (flags) |f| {
        if (std.mem.eql(u8, f, name)) return true;
        if (std.mem.startsWith(u8, f, name) and f.len > name.len and f[name.len] == '=') return true;
    }
    return false;
}

fn envFlag(env: std.process.EnvMap, name: []const u8) bool {
    const val = env.get(name) orelse return false;
    if (std.ascii.eqlIgnoreCase(val, "1")) return true;
    if (std.ascii.eqlIgnoreCase(val, "true")) return true;
    if (std.ascii.eqlIgnoreCase(val, "yes")) return true;
    if (std.ascii.eqlIgnoreCase(val, "y")) return true;
    if (std.ascii.eqlIgnoreCase(val, "on")) return true;
    return false;
}

fn parseVersion(v: []const u8) [3]u32 {
    var result = [3]u32{ 0, 0, 0 };
    var it = std.mem.tokenizeAny(u8, v, "v.");
    var idx: usize = 0;
    while (it.next()) |part| {
        if (idx >= 3) break;
        var num: u32 = 0;
        var found_digit = false;
        for (part) |c| {
            if (c >= '0' and c <= '9') {
                num = num * 10 + (c - '0');
                found_digit = true;
            } else break;
        }
        if (found_digit) {
            result[idx] = num;
            idx += 1;
        }
    }
    return result;
}

fn versionSatisfies(version: [3]u32, minimum: [3]u32) bool {
    if (version[0] != minimum[0]) return version[0] > minimum[0];
    if (version[1] != minimum[1]) return version[1] > minimum[1];
    return version[2] >= minimum[2];
}

fn probeNode(allocator: std.mem.Allocator, path: []const u8) ?[3]u32 {
    const result = std.process.Child.run(.{
        .allocator = allocator,
        .argv = &[_][]const u8{ path, "--version" },
    }) catch return null;
    defer allocator.free(result.stdout);
    defer allocator.free(result.stderr);
    if (result.term != .Exited) return null;
    if (result.term.Exited != 0) return null;
    const out = std.mem.trim(u8, result.stdout, " \t\r\n");
    if (out.len == 0) return null;
    return parseVersion(out);
}

fn findNodeBinary(allocator: std.mem.Allocator, env: std.process.EnvMap) ?[]const u8 {
    if (env.get("PAPERCLIP_NODE")) |p| {
        if (probeNode(allocator, p) != null) {
            return allocator.dupe(u8, p) catch null;
        }
    }
    if (std.fs.selfExeDirPathAlloc(allocator) catch null) |exe_dir| {
        defer allocator.free(exe_dir);
        const candidates = [_] ?[]const u8{
            std.fs.path.join(allocator, &[_][]const u8{ exe_dir, "runtime", "node", if (builtin.os.tag == .windows) "node.exe" else "bin/node" }) catch null,
            std.fs.path.join(allocator, &[_][]const u8{ exe_dir, "runtime", if (builtin.os.tag == .windows) "node.exe" else "node" }) catch null,
            std.fs.path.join(allocator, &[_][]const u8{ exe_dir, if (builtin.os.tag == .windows) "node.exe" else "node" }) catch null,
            std.fs.path.join(allocator, &[_][]const u8{ exe_dir, "payload", "runtime", "node", if (builtin.os.tag == .windows) "node.exe" else "bin/node" }) catch null,
        };
        for (candidates) |cand_opt| {
            if (cand_opt) |cand| {
                defer allocator.free(cand);
                if (probeNode(allocator, cand) != null) {
                    return allocator.dupe(u8, cand) catch null;
                }
            }
        }
    }
    const path_env = env.get("PATH") orelse "";
    var it = std.mem.splitScalar(u8, path_env, if (builtin.os.tag == .windows) ';' else ':');
    while (it.next()) |dir| {
        if (dir.len == 0) continue;
        const candidate = std.fs.path.join(allocator, &[_][]const u8{ dir, if (builtin.os.tag == .windows) "node.exe" else "node" }) catch continue;
        defer allocator.free(candidate);
        if (probeNode(allocator, candidate) != null) {
            return allocator.dupe(u8, candidate) catch null;
        }
    }
    return null;
}

fn resolveToAbsolute(allocator: std.mem.Allocator, path: []const u8) ?[]u8 {
    if (std.fs.cwd().realpathAlloc(allocator, path) catch null) |abs| {
        return abs;
    }
    if (std.fs.path.isAbsolute(path)) {
        return allocator.dupe(u8, path) catch null;
    }
    const cwd = std.fs.cwd().realpathAlloc(allocator, ".") catch return null;
    defer allocator.free(cwd);
    return std.fs.path.join(allocator, &[_][]const u8{ cwd, path }) catch null;
}

fn findPayload(allocator: std.mem.Allocator, env: std.process.EnvMap) ?struct { root: []u8, entry: []u8, node_modules: ?[]u8 } {
    // Check PAPERCLIP_ENTRY - resolve to absolute
    if (env.get("PAPERCLIP_ENTRY")) |entry| {
        if (std.fs.cwd().statFile(entry) catch null) |s| {
            if (s.kind == .file) {
                const abs_entry = resolveToAbsolute(allocator, entry) orelse return null;
                const parent = std.fs.path.dirname(abs_entry) orelse ".";
                const grandparent = std.fs.path.dirname(parent) orelse parent;
                const abs_root = resolveToAbsolute(allocator, grandparent) orelse allocator.dupe(u8, grandparent) catch return null;
                return .{
                    .root = abs_root,
                    .entry = abs_entry,
                    .node_modules = null,
                };
            }
        }
    }
    // Check PAPERCLIP_PAYLOAD - resolve to absolute
    if (env.get("PAPERCLIP_PAYLOAD")) |payload_root| {
        const abs_payload_root = resolveToAbsolute(allocator, payload_root) orelse {
            // fallback to original if resolve fails
            if (std.fs.path.isAbsolute(payload_root)) {
                return null; // will try anchors
            }
            return null;
        };
        defer {
            // Keep abs_payload_root alive via dupe in return, free temp if not returned
        }
        const entry_candidates = [_][]const u8{
            "app/index.js",
            "app/index.mjs",
            "index.js",
            "cli/dist/index.js",
            "dist/index.js",
        };
        for (entry_candidates) |rel| {
            const full = std.fs.path.join(allocator, &[_][]const u8{ abs_payload_root, rel }) catch continue;
            defer allocator.free(full);
            if (std.fs.cwd().statFile(full) catch null) |s| {
                if (s.kind == .file) {
                    const nm_path = std.fs.path.join(allocator, &[_][]const u8{ abs_payload_root, "node_modules" }) catch null;
                    var nm_exists: ?[]u8 = null;
                    if (nm_path) |nm| {
                        defer allocator.free(nm);
                        if (std.fs.cwd().statFile(nm) catch null) |ns| {
                            if (ns.kind == .directory) {
                                nm_exists = allocator.dupe(u8, nm) catch null;
                            }
                        }
                    }
                    // Need to dupe abs_payload_root and full for return
                    const root_dup = allocator.dupe(u8, abs_payload_root) catch {
                        allocator.free(abs_payload_root);
                        return null;
                    };
                    const entry_dup = allocator.dupe(u8, full) catch {
                        allocator.free(root_dup);
                        allocator.free(abs_payload_root);
                        return null;
                    };
                    allocator.free(abs_payload_root);
                    return .{
                        .root = root_dup,
                        .entry = entry_dup,
                        .node_modules = nm_exists,
                    };
                }
            }
        }
        allocator.free(abs_payload_root);
    }

    // Search anchors: exe dir, cwd (both absolute)
    var anchors = std.ArrayList([]u8).init(allocator);
    defer {
        for (anchors.items) |a| allocator.free(a);
        anchors.deinit();
    }

    if (std.fs.selfExeDirPathAlloc(allocator) catch null) |exe_dir| {
        anchors.append(exe_dir) catch {};
    }
    if (std.fs.cwd().realpathAlloc(allocator, ".") catch null) |cwd| {
        anchors.append(cwd) catch {};
    }

    const payload_relpaths = [_][]const u8{ "payload", "_internal/payload", "." };
    const entry_relpaths = [_][]const u8{ "app/index.js", "app/index.mjs", "index.js", "cli/dist/index.js", "dist/index.js" };

    for (anchors.items) |anchor| {
        for (payload_relpaths) |prel| {
            const candidate_root = std.fs.path.join(allocator, &[_][]const u8{ anchor, prel }) catch continue;
            defer allocator.free(candidate_root);
            for (entry_relpaths) |erel| {
                const entry_path = std.fs.path.join(allocator, &[_][]const u8{ candidate_root, erel }) catch continue;
                defer allocator.free(entry_path);
                if (std.fs.cwd().statFile(entry_path) catch null) |s| {
                    if (s.kind == .file) {
                        const nm_path = std.fs.path.join(allocator, &[_][]const u8{ candidate_root, "node_modules" }) catch null;
                        var nm_exists: ?[]u8 = null;
                        if (nm_path) |nm| {
                            defer allocator.free(nm);
                            if (std.fs.cwd().statFile(nm) catch null) |ns| {
                                if (ns.kind == .directory) {
                                    nm_exists = allocator.dupe(u8, nm) catch null;
                                }
                            }
                        }
                        return .{
                            .root = allocator.dupe(u8, candidate_root) catch return null,
                            .entry = allocator.dupe(u8, entry_path) catch return null,
                            .node_modules = nm_exists,
                        };
                    }
                }
            }
        }
    }
    return null;
}

fn findPostgresPaths(allocator: std.mem.Allocator, node_modules: ?[]const u8) !std.ArrayList([]u8) {
    var list = std.ArrayList([]u8).init(allocator);
    if (node_modules == null) return list;
    const nm = node_modules.?;

    const slugs = [_][]const u8{ "windows-x64", "linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64" };
    for (slugs) |slug| {
        const pkg_dir = try std.fs.path.join(allocator, &[_][]const u8{ nm, "@embedded-postgres", slug });
        defer allocator.free(pkg_dir);
        if (std.fs.cwd().statFile(pkg_dir) catch null) |s| {
            if (s.kind == .directory) {
                const bin_dir = try std.fs.path.join(allocator, &[_][]const u8{ pkg_dir, "native", "bin" });
                if (std.fs.cwd().statFile(bin_dir) catch null) |bs| {
                    if (bs.kind == .directory) {
                        try list.append(try allocator.dupe(u8, bin_dir));
                    }
                }
                allocator.free(bin_dir);
                const lib_dir = try std.fs.path.join(allocator, &[_][]const u8{ pkg_dir, "native", "lib" });
                if (std.fs.cwd().statFile(lib_dir) catch null) |ls| {
                    if (ls.kind == .directory) {
                        try list.append(try allocator.dupe(u8, lib_dir));
                    }
                }
                allocator.free(lib_dir);
            }
        }
    }
    return list;
}

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    const args = try std.process.argsAlloc(allocator);
    defer std.process.argsFree(allocator, args);

    const user_args = if (args.len > 1) args[1..] else &[_][]const u8{};

    var launcher_flags = std.ArrayList([]const u8).init(allocator);
    defer launcher_flags.deinit();
    var app_args = std.ArrayList([]const u8).init(allocator);
    defer app_args.deinit();
    var i: usize = 0;
    while (i < user_args.len) : (i += 1) {
        const arg = user_args[i];
        if (std.mem.eql(u8, arg, "--")) {
            i += 1;
            break;
        }
        if (isLauncherFlag(arg)) {
            try launcher_flags.append(arg);
            const base = if (std.mem.indexOf(u8, arg, "=")) |idx| arg[0..idx] else arg;
            var takes_value = false;
            for (LAUNCHER_FLAGS_WITH_VALUE) |f| {
                if (std.mem.eql(u8, base, f)) {
                    takes_value = true;
                    break;
                }
            }
            if (takes_value and std.mem.indexOf(u8, arg, "=") == null) {
                if (i + 1 < user_args.len and !std.mem.eql(u8, user_args[i + 1], "--")) {
                    i += 1;
                    try launcher_flags.append(user_args[i]);
                }
            }
            continue;
        }
        break;
    }
    while (i < user_args.len) : (i += 1) {
        try app_args.append(user_args[i]);
    }

    if (hasFlag(launcher_flags.items, "--launcher-help")) {
        std.debug.print(
            \\paperclip launcher {s} (Zig - low AV false positives)
            \\
            \\Runs the real Paperclip CLI/app from a single self-contained executable.
            \\
            \\Usage
            \\  paperclip [launcher-flag] [--] <any paperclipai arguments...>
            \\
            \\Launcher flags
            \\  --launcher-info          Print resolved Node runtime and payload as JSON.
            \\  --launcher-doctor        Diagnose this installation.
            \\  --launcher-fetch-node    Permit downloading a portable Node if none qualifies.
            \\  --launcher-node PATH     Use PATH as the Node binary.
            \\  --launcher-payload PATH  Use PATH as the payload root.
            \\  --launcher-version       Print launcher and app versions.
            \\  --launcher-help          Show this help.
            \\
            \\Double-click: If no arguments are given, runs 'doctor' to check installation.
            \\
            \\Environment
            \\  PAPERCLIP_NODE            Explicit Node binary to use.
            \\  PAPERCLIP_PAYLOAD         Explicit payload root.
            \\  PAPERCLIP_ENTRY           Explicit entry JS file.
            \\  PAPERCLIP_DATA_DIR        Override the per-user data directory.
            \\  PAPERCLIP_FETCH_NODE=1    Permit downloading a portable Node at runtime.
            \\  PAPERCLIP_NODE_OPTIONS    Extra Node flags for the app.
            \\  PAPERCLIP_LAUNCHER_DEBUG=1  Trace launcher decisions on stderr.
            \\
            \\Examples
            \\  paperclip doctor
            \\  paperclip onboard
            \\  paperclip issue list --company acme
            \\
        , .{LAUNCHER_VERSION});
        return;
    }

    var env = try std.process.getEnvMap(allocator);
    defer env.deinit();

    if (flagValue(launcher_flags.items, "--launcher-node")) |node_path| {
        try env.put("PAPERCLIP_NODE", node_path);
    }
    if (flagValue(launcher_flags.items, "--launcher-payload")) |payload_path| {
        try env.put("PAPERCLIP_PAYLOAD", payload_path);
    }

    const payload_opt = findPayload(allocator, env);
    if (payload_opt == null) {
        std.debug.print("Could not find Paperclip payload.\n", .{});
        std.debug.print("Searched beside executable and in current directory.\n", .{});
        std.debug.print("Set PAPERCLIP_PAYLOAD to the payload root (dir containing app/index.js)\n", .{});
        std.debug.print("Run with --launcher-doctor for diagnosis.\n", .{});
        std.process.exit(78);
    }
    const p = payload_opt.?;

    const node_bin_opt = findNodeBinary(allocator, env);
    if (node_bin_opt == null) {
        std.debug.print("Paperclip needs Node.js >= {s} and no suitable runtime was found.\n", .{MINIMUM_NODE_VERSION});
        std.debug.print("Install Node.js from https://nodejs.org/ or set PAPERCLIP_NODE\n", .{});
        std.process.exit(78);
    }
    const node_path = node_bin_opt.?;

    if (env.get("PAPERCLIP_ALLOW_OLD_NODE") == null) {
        if (probeNode(allocator, node_path)) |ver| {
            const min_ver = parseVersion(MINIMUM_NODE_VERSION);
            if (!versionSatisfies(ver, min_ver)) {
                std.debug.print("Node version {d}.{d}.{d} is below required {s}\n", .{ ver[0], ver[1], ver[2], MINIMUM_NODE_VERSION });
                std.debug.print("Set PAPERCLIP_ALLOW_OLD_NODE=1 to bypass this check for testing.\n", .{});
                std.process.exit(78);
            }
        }
    }

    if (hasFlag(launcher_flags.items, "--launcher-version")) {
        std.debug.print("paperclip-launcher {s} (Zig)\n", .{LAUNCHER_VERSION});
        if (probeNode(allocator, node_path)) |ver| {
            std.debug.print("node {d}.{d}.{d} at {s}\n", .{ ver[0], ver[1], ver[2], node_path });
        }
        std.debug.print("payload at {s}\n", .{p.root});
        return;
    }

    if (hasFlag(launcher_flags.items, "--launcher-info")) {
        std.debug.print("{{\"launcher_version\":\"{s}\",\"node\":\"{s}\",\"payload_root\":\"{s}\",\"payload_entry\":\"{s}\"}}\n", .{ LAUNCHER_VERSION, node_path, p.root, p.entry });
        return;
    }

    if (hasFlag(launcher_flags.items, "--launcher-doctor")) {
        std.debug.print("Paperclip launcher doctor (Zig)\n", .{});
        std.debug.print("  launcher version : {s}\n", .{LAUNCHER_VERSION});
        std.debug.print("  node             : {s}\n", .{node_path});
        std.debug.print("  payload root     : {s}\n", .{p.root});
        std.debug.print("  payload entry    : {s}\n", .{p.entry});
        if (p.node_modules) |nm| {
            std.debug.print("  node_modules     : {s}\n", .{nm});
        }
        const pg_paths = try findPostgresPaths(allocator, p.node_modules);
        defer {
            for (pg_paths.items) |pp| allocator.free(pp);
            pg_paths.deinit();
        }
        if (pg_paths.items.len > 0) {
            std.debug.print("  postgres paths   :\n", .{});
            for (pg_paths.items) |pp| {
                std.debug.print("    - {s}\n", .{pp});
            }
        } else {
            std.debug.print("  postgres         : not found (will need DATABASE_URL)\n", .{});
        }
        return;
    }

    const pg_paths = try findPostgresPaths(allocator, p.node_modules);
    defer {
        for (pg_paths.items) |pp| allocator.free(pp);
        pg_paths.deinit();
    }

    if (pg_paths.items.len > 0) {
        const old_path = env.get("PATH") orelse "";
        var new_path = std.ArrayList(u8).init(allocator);
        defer new_path.deinit();
        for (pg_paths.items) |pp| {
            try new_path.appendSlice(pp);
            try new_path.append(if (builtin.os.tag == .windows) ';' else ':');
        }
        if (std.fs.path.dirname(node_path)) |node_dir| {
            try new_path.appendSlice(node_dir);
            try new_path.append(if (builtin.os.tag == .windows) ';' else ':');
        }
        try new_path.appendSlice(old_path);
        try env.put("PATH", new_path.items);
    } else {
        const old_path = env.get("PATH") orelse "";
        if (std.fs.path.dirname(node_path)) |node_dir| {
            if (std.mem.indexOf(u8, old_path, node_dir) == null) {
                var new_path = std.ArrayList(u8).init(allocator);
                defer new_path.deinit();
                try new_path.appendSlice(node_dir);
                try new_path.append(if (builtin.os.tag == .windows) ';' else ':');
                try new_path.appendSlice(old_path);
                try env.put("PATH", new_path.items);
            }
        }
    }

    if (p.node_modules) |nm| {
        const old_node_path = env.get("NODE_PATH") orelse "";
        var new_node_path = std.ArrayList(u8).init(allocator);
        defer new_node_path.deinit();
        try new_node_path.appendSlice(nm);
        if (old_node_path.len > 0) {
            try new_node_path.append(if (builtin.os.tag == .windows) ';' else ':');
            try new_node_path.appendSlice(old_node_path);
        }
        try env.put("NODE_PATH", new_node_path.items);
    }

    try env.put("PAPERCLIP_EXE_LAUNCHER", "1");
    try env.put("PAPERCLIP_LAUNCHER_VERSION", LAUNCHER_VERSION);

    var default_args = [_][]const u8{"doctor"};
    var final_app_args: []const []const u8 = app_args.items;
    if (app_args.items.len == 0) {
        final_app_args = &default_args;
    }

    var argv = std.ArrayList([]const u8).init(allocator);
    defer argv.deinit();
    try argv.append(node_path);
    if (env.get("PAPERCLIP_NODE_OPTIONS")) |opts| {
        var it = std.mem.splitScalar(u8, opts, ' ');
        while (it.next()) |opt| {
            if (opt.len > 0) try argv.append(opt);
        }
    }
    try argv.append(p.entry);
    for (final_app_args) |a| {
        try argv.append(a);
    }

    var child = std.process.Child.init(argv.items, allocator);
    child.env_map = &env;
    child.stdin_behavior = .Inherit;
    child.stdout_behavior = .Inherit;
    child.stderr_behavior = .Inherit;
    // Use payload root as cwd, not entry dir, to avoid double-join bugs with relative paths
    // Python launcher uses entry.parent, but with absolute entry that is safe. We use root for safety.
    child.cwd = p.root;

    const term = child.spawnAndWait() catch |err| {
        std.debug.print("Failed to start Node: {any}\n", .{err});
        std.process.exit(78);
    };

    switch (term) {
        .Exited => |code| std.process.exit(code),
        .Signal => |sig| std.process.exit(128 + @as(u8, @intCast(sig))),
        .Stopped => |sig| std.process.exit(128 + @as(u8, @intCast(sig))),
        .Unknown => |code| std.process.exit(@as(u8, @intCast(code))),
    }
}
