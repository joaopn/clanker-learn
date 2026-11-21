"""The parts of the core and of the sandbox's own programs that are decided without a daemon."""
import io
import os
import subprocess
import tarfile
import time

import core
import files_server
import loopback_fwd
import pytest
import sandbox_init


@pytest.mark.parametrize("name", ["a", "kafka", "learn-kafka-2", "0day", "a" * 31])
def test_a_project_name_is_short_lower_case_and_starts_with_a_letter_or_digit(name):
    assert core.valid_name(name) == name


@pytest.mark.parametrize("name", ["", "..", "a/b", "Kafka", "-kafka", "kafka-", "kafka\n", "a" * 32, "a_b", "a.b", "a b", "../../xtables", None, 7])
def test_any_other_name_is_refused(name):
    with pytest.raises(core.CoreError):
        core.valid_name(name)
    with pytest.raises(core.CoreError):
        core.paths(name)


@pytest.mark.parametrize("operation", [core.create, core.start, core.stop, core.recreate, core.set_limits, core.destroy, core.status])
@pytest.mark.parametrize("name", ["../../xtables", "a/b", "..", ""])
def test_no_operation_makes_a_path_or_a_lock_from_a_name_that_is_not_one(monkeypatch, tmp_path, operation, name):
    monkeypatch.setattr(core, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "run" / "locks"))
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: pytest.fail("the daemon was asked"))
    with pytest.raises(core.CoreError, match="A project name is"):
        operation(name)
    assert [str(found.relative_to(tmp_path)) for found in tmp_path.rglob("*") if found.is_file()] == []


def test_every_path_and_docker_name_of_a_project_is_built_from_its_name(monkeypatch):
    monkeypatch.setattr(core, "DATA", "/data")
    assert core.paths("kafka") == {
        "record": "/data/core/projects/kafka.json",
        "workspace": "/data/projects/kafka",
        "home": "/data/homes/kafka",
        "provision": "/data/core/provision/kafka",
        "host_keys": "/data/core/keys/kafka",
        "tutor_keys": "/data/tutor/keys/kafka",
        "container": "cl-sb-kafka",
        "network": "cl-net-kafka",
    }


def test_the_image_tag_follows_the_definition_and_nothing_else(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM x\n")
    (tmp_path / "init.py").write_text("print(1)\n")
    tag = core.image_tag(str(tmp_path))
    assert tag.startswith("cl-sandbox:") and len(tag) == len("cl-sandbox:") + 12
    assert core.image_tag(str(tmp_path)) == tag
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "init.cpython-313.pyc").write_bytes(b"compiled")
    (tmp_path / "stray.pyc").write_bytes(b"compiled")
    assert core.image_tag(str(tmp_path)) == tag
    (tmp_path / "init.py").write_text("print(2)\n")
    changed = core.image_tag(str(tmp_path))
    assert changed != tag
    (tmp_path / "init.py").rename(tmp_path / "other.py")
    assert core.image_tag(str(tmp_path)) not in (tag, changed)


def test_a_sandbox_is_created_plain_fenced_and_with_its_three_mounts(monkeypatch):
    monkeypatch.setattr(core, "DATA", "/data")
    record = core.new_record("kafka", {"pids": 512, "memory": 2 * 1024 ** 3, "cpus": 1.5})
    # The whole list, so that nothing can be added to it unseen: no privilege, no restart
    # policy, no published port, no added capability, no namespace of the outer container.
    assert core.container_args(record, "cl-sandbox:abc") == [
        "--name", "cl-sb-kafka", "--hostname", "kafka", "--network", "cl-net-kafka",
        "--label", "cl.project=kafka",
        "--cap-drop", "NET_RAW", "--sysctl", "net.ipv4.ping_group_range=0 2147483647",
        "-v", "/data/projects/kafka:/workspace",
        "-v", "/data/homes/kafka:/home/learner",
        "-v", "/data/core/provision/kafka:/etc/cl-provision:ro",
        "--pids-limit", "512",
        "--memory", "2147483648", "--memory-swap", "2147483648",
        "--cpu-period", "100000", "--cpu-quota", "150000",
        "cl-sandbox:abc",
    ]


def test_limits_are_given_so_that_a_later_update_can_change_them():
    none = {"pids": None, "memory": None, "cpus": None}
    assert core.limit_args(none) == ["--pids-limit", "-1"]
    assert core.limit_args(none, for_update=True) == ["--pids-limit", "-1", "--cpu-quota", "-1"]
    some = {"pids": 100, "memory": 1024 ** 3, "cpus": 0.5}
    assert core.limit_args(some, for_update=True) == core.limit_args(some)


@pytest.mark.parametrize("limits", [{"pids": 3}, {"memory": 1000}, {"cpus": 0}, {"cpus": -1}, {"pids": "many"}, {"disk": 5},
                                    {"pids": float("inf")}, {"memory": float("inf")}, {"cpus": float("inf")}, {"cpus": float("nan")}])
def test_a_limit_that_makes_no_sense_is_refused(limits):
    with pytest.raises(core.CoreError):
        core.checked_limits(limits)


def test_the_default_limits_are_a_pids_limit_only():
    assert core.checked_limits(None) == {"pids": 16384, "memory": None, "cpus": None}


@pytest.mark.parametrize("text, size", [("512m", 512 * 1024 ** 2), ("2g", 2 * 1024 ** 3), ("1.5G", 1536 * 1024 ** 2), ("1048576", 1048576), ("none", None)])
def test_a_size_is_written_as_people_write_it(text, size):
    assert core.parse_size(text) == size


@pytest.mark.parametrize("text", ["lots", "infg", "g", "", "1.5.2m"])
def test_a_size_that_is_not_one_is_refused(text):
    with pytest.raises(core.CoreError):
        core.parse_size(text)


def test_a_record_is_read_back_as_written(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    record = core.new_record("kafka", {"memory": 1024 ** 3})
    core.write_record(record)
    assert core.read_record("kafka") == record
    assert core.recorded_names() == ["kafka"]
    with pytest.raises(core.CoreError, match="no project named"):
        core.read_record("other")


@pytest.mark.parametrize("text", [" ", "{", '{"name": "other", "wanted": "running", "limits": {}}', '{"name": "kafka", "wanted": "maybe", "limits": {}}',
                                  '{"name": "kafka", "wanted": "running", "limits": {"pids": 1}}', '{"name": "kafka"}', "[]",
                                  '{"name": "kafka", "wanted": "running", "limits": {}}',                       # no "created"
                                  '{"name": "kafka", "wanted": "running", "created": "x", "limits": {"pids": Infinity}}'])
def test_a_record_that_is_not_this_projects_is_unreadable_not_a_crash(monkeypatch, tmp_path, text):
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    (tmp_path / "core" / "projects").mkdir(parents=True)
    (tmp_path / "core" / "projects" / "kafka.json").write_text(text)
    with pytest.raises(core.CoreError, match="cannot be read"):
        core.read_record("kafka")


def test_start_wanted_starts_what_should_run_and_goes_on_past_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    for name, wanted in (("a-runs", "running"), ("b-broken", "running"), ("c-stopped", "stopped"), ("d-runs", "running"),
                         ("f-half-destroyed", "destroying"), ("g-faulty", "running"), ("h-runs", "running")):
        core.write_record(dict(core.new_record(name), wanted=wanted))
    (tmp_path / "core" / "projects" / "e-unreadable.json").write_text("{")
    asked = []

    def start(name, say):
        asked.append(name)
        if name == "b-broken":
            raise core.CoreError("no luck")
        if name == "g-faulty":
            raise PermissionError("a fault nobody planned for")
    monkeypatch.setattr(core, "start", start)
    out = core.start_wanted()
    assert asked == ["a-runs", "b-broken", "d-runs", "g-faulty", "h-runs"]
    assert out["started"] == ["a-runs", "d-runs", "h-runs"]
    assert sorted(out["failed"]) == ["b-broken", "e-unreadable", "g-faulty"]


def answer(code, stderr="", stdout=""):
    return subprocess.CompletedProcess(["docker"], code, stdout, stderr)


@pytest.mark.parametrize("stderr", [
    "Error response from daemon: No such container: cl-sb-kafka",
    "Error response from daemon: No such image: cl-sandbox:abc",
    "Error response from daemon: network cl-net-kafka not found",
    "Error response from daemon: No such network: cl-net-kafka",
])
def test_what_the_daemon_says_is_not_there_is_not_there(monkeypatch, stderr):
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, stderr))
    assert core.there_is("container", "cl-sb-kafka") is False and core.inspect_container("cl-sb-kafka") is None


@pytest.mark.parametrize("stderr", [
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
    "error during connect: dial unix /var/run/docker.sock: connect: no such file or directory",
    "Error response from daemon: something else entirely",
    "",
])
def test_a_daemon_that_does_not_answer_is_an_error_never_not_there(monkeypatch, stderr):
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, stderr))
    with pytest.raises(core.CoreError, match="could not say whether"):
        core.there_is("container", "cl-sb-kafka")
    with pytest.raises(core.CoreError):
        core.inspect_container("cl-sb-kafka")


def test_a_silent_daemon_stops_a_destroy_before_anything_is_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, "Cannot connect to the Docker daemon"))
    core.write_record(core.new_record("kafka"))
    (tmp_path / "projects" / "kafka").mkdir(parents=True)
    (tmp_path / "projects" / "kafka" / "work").write_text("the learner's")
    with pytest.raises(core.CoreError):
        core.destroy("kafka")
    assert (tmp_path / "projects" / "kafka" / "work").read_text() == "the learner's"
    assert core.read_record("kafka")["wanted"] == "destroying"


def test_a_destroy_removes_whatever_is_in_the_way_and_follows_no_link(monkeypatch, tmp_path):
    """With the daemon saying there is no container and no network: a folder that is a file, a
    folder that is a link, and a link inside a folder are removed, and what the links point at
    is left."""
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, "Error response from daemon: No such container: x"))
    core.write_record(core.new_record("kafka"))
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "kept").write_text("kept")
    p = core.paths("kafka")
    os.makedirs(os.path.dirname(p["home"]))
    with open(p["home"], "w") as fh:
        fh.write("a file where a folder should be")
    os.makedirs(os.path.dirname(p["provision"]), exist_ok=True)
    os.symlink(precious, p["provision"])
    os.makedirs(p["workspace"])
    os.symlink(precious, os.path.join(p["workspace"], "link"))
    assert core.destroy("kafka") == {"name": "kafka", "state": "destroyed"}
    assert not any(os.path.lexists(p[key]) for key in core.FOLDERS) and not os.path.exists(p["record"])
    assert (precious / "kept").read_text() == "kept"


def test_a_destroy_works_where_the_record_cannot_be_rewritten(monkeypatch, tmp_path):
    """A full volume: the mark cannot be written. Removing the project is the way out of a full
    volume, so the destroy goes on; and if it is then cut short, nothing starts the project."""
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    core.write_record(core.new_record("kafka"))
    (tmp_path / "projects" / "kafka").mkdir(parents=True)

    def full(record):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(core, "write_record", full)
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, "Cannot connect to the Docker daemon"))
    with pytest.raises(core.CoreError):
        core.destroy("kafka")                      # cut short: the daemon does not answer
    with pytest.raises(core.CoreError, match="is being destroyed. `destroy kafka` finishes it"):
        core.read_record("kafka")                  # emptied in place: nothing will start it
    monkeypatch.setattr(core, "start", lambda name, say: pytest.fail("a project being destroyed was started"))
    assert core.start_wanted() == {"started": [], "failed": {}}   # passed over, like a marked one
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, "Error response from daemon: No such container: x"))
    (tmp_path / "core" / "projects" / "kafka.json.tmp").write_text("left by a write that was cut short")
    assert core.destroy("kafka") == {"name": "kafka", "state": "destroyed"}
    assert list((tmp_path / "core" / "projects").iterdir()) == [] and not (tmp_path / "projects" / "kafka").exists()


def test_a_creation_that_fails_is_marked_before_it_is_cleaned_up(monkeypatch, tmp_path):
    """Cut short in the clean-up, it must not be finished as a creation at the next start."""
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    monkeypatch.setattr(core, "leftovers", lambda name: [])
    monkeypatch.setattr(core, "ensure", lambda record, say: (_ for _ in ()).throw(core.CoreError("the image could not be built")))
    seen = []

    def cut_short(name):
        seen.append(core.read_record(name)["wanted"])
        raise core.CoreError("killed here")
    monkeypatch.setattr(core, "remove_everything", cut_short)
    with pytest.raises(core.CoreError, match="could not be built"):
        core.create("kafka")
    assert seen == ["destroying"] and core.read_record("kafka")["wanted"] == "destroying"


def test_a_creation_whose_record_cannot_be_written_leaves_no_record(monkeypatch, tmp_path):
    """The record lands and the write still fails (the folder cannot be synced): the caller is
    told it failed, so nothing may be left for the next start to finish."""
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, "Error response from daemon: No such container: x"))
    really_write, calls = core.write_record, []

    def write_then_fail_once(record):
        calls.append(record["wanted"])
        really_write(record)
        if len(calls) == 1:
            raise OSError(5, "Input/output error")
    monkeypatch.setattr(core, "write_record", write_then_fail_once)
    with pytest.raises(OSError):
        core.create("kafka")
    assert calls == ["running", "destroying"]
    assert core.recorded_names() == [] and list((tmp_path / "core" / "projects").iterdir()) == []


def test_the_list_tells_an_unreadable_record_from_a_daemon_that_does_not_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    core.write_record(core.new_record("kafka"))
    (tmp_path / "core" / "projects" / "broken.json").write_text("{")
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, "Cannot connect to the Docker daemon"))
    listed = {one["name"]: one for one in core.list_projects()}
    assert listed["broken"]["state"] == "unreadable"
    assert listed["kafka"]["state"] == "unknown" and listed["kafka"]["wanted"] == "running" and "could not say" in listed["kafka"]["problem"]
    # Whoever reads the list finds the same fields as in a status, whatever the daemon says.
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, "Error response from daemon: No such container: x"))
    assert set(listed["kafka"]) == set(core.status("kafka")) | {"problem"}


def test_a_project_being_destroyed_is_not_started_stopped_or_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: pytest.fail("the daemon was asked"))
    core.write_record(dict(core.new_record("kafka"), wanted="destroying"))
    for operation in (core.start, core.stop, core.recreate, core.set_limits):
        with pytest.raises(core.CoreError, match="is being destroyed"):
            operation("kafka")
    with pytest.raises(core.CoreError, match="exists"):
        core.create("kafka")


def test_half_a_key_pair_is_made_again(monkeypatch, tmp_path):
    """What a killed ssh-keygen leaves: the private half alone. It must not be taken for a key."""
    made = []

    def keygen(command, **kwargs):
        path = command[command.index("-f") + 1]
        assert not os.path.exists(path) and not os.path.exists(path + ".pub")
        made.append(path)
        for half in (path, path + ".pub"):
            with open(half, "w") as fh:
                fh.write("new")
        return answer(0)
    monkeypatch.setattr(core.subprocess, "run", keygen)
    key = tmp_path / "keys" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("half")
    core.keygen(str(key), "comment")
    assert made == [str(key)] and key.read_text() == "new"
    core.keygen(str(key), "comment")
    assert made == [str(key)]   # a whole pair is left alone
    (tmp_path / "keys" / "id_ed25519.pub").write_text("")
    core.keygen(str(key), "comment")
    assert made == [str(key)] * 2   # an empty half is not a half


def test_the_processor_limit_in_force_is_read_from_the_outer_containers_own_tree(tmp_path):
    assert core.cpu_limit_in_force("abc", root=str(tmp_path)) is None
    (tmp_path / "docker" / "abc").mkdir(parents=True)
    (tmp_path / "docker" / "abc" / "cpu.max").write_text("150000 100000\n")
    assert core.cpu_limit_in_force("abc", root=str(tmp_path)) == "150000 100000\n"


# --- the files server ------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "sub").mkdir(parents=True)
    (root / "plot.png").write_bytes(b"png")
    (root / "sub" / "page.html").write_text("<p>hi</p>")
    (tmp_path / "secret").write_text("outside")
    (root / "leak").symlink_to(tmp_path / "secret")
    (root / "inside").symlink_to(root / "plot.png")
    return root


@pytest.mark.parametrize("url, served", [
    ("/plot.png", "plot.png"), ("/sub/page.html", "sub/page.html"), ("/sub/../plot.png", "plot.png"),
    ("/plot.png?x=1", "plot.png"), ("/inside", "plot.png"), ("/sub/%70age.html", "sub/page.html"),
])
def test_a_file_in_the_workspace_is_served(workspace, url, served):
    assert files_server.resolve(str(workspace), url) == str(workspace / served)


@pytest.mark.parametrize("url", ["/leak", "/../secret", "/%2e%2e/secret", "/sub/../../secret", "//../secret", "/", "/sub", "/sub/", "/missing", "/plot.png%00", ""])
def test_nothing_outside_the_workspace_and_no_folder_is_served(workspace, url):
    assert (workspace.parent / "secret").read_text() == "outside"  # what the refused paths point at exists
    assert files_server.resolve(str(workspace), url) is None


# --- the sandbox's init ----------------------------------------------------------------------

def test_a_new_home_is_filled_and_a_used_one_is_left_alone(tmp_path):
    skeleton, home = tmp_path / "skel", tmp_path / "home"
    skeleton.mkdir()
    home.mkdir()
    (skeleton / ".bashrc").write_text("# skeleton\n")
    assert sandbox_init.fill_home(str(home), str(skeleton), os.getuid(), os.getgid()) is True
    assert (home / ".bashrc").read_text() == "# skeleton\n"
    (home / ".bashrc").write_text("# mine\n")
    assert sandbox_init.fill_home(str(home), str(skeleton), os.getuid(), os.getgid()) is False
    assert (home / ".bashrc").read_text() == "# mine\n"


def test_provisioning_installs_the_key_and_the_sshd_settings(tmp_path):
    provision, home, config = tmp_path / "provision", tmp_path / "home", tmp_path / "etc" / "cl.conf"
    provision.mkdir()
    home.mkdir()
    (provision / "authorized_keys").write_text("ssh-ed25519 AAAA tutor\n")
    (provision / "sshd_config").write_text("AllowUsers learner\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("not a key")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "authorized_keys").symlink_to(elsewhere)  # a link the sandbox user planted
    sandbox_init.install_provisioning(str(provision), str(home), os.getuid(), os.getgid(), sshd_config=str(config))
    key = home / ".ssh" / "authorized_keys"
    assert key.read_text() == "ssh-ed25519 AAAA tutor\n" and not key.is_symlink()
    assert elsewhere.read_text() == "not a key"
    assert oct(key.stat().st_mode & 0o777) == "0o600" and oct((home / ".ssh").stat().st_mode & 0o777) == "0o700"
    assert config.read_text() == "AllowUsers learner\n"


def test_provisioning_with_an_empty_folder_installs_nothing_and_removes_nothing(tmp_path):
    provision, home = tmp_path / "provision", tmp_path / "home"
    provision.mkdir()
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "authorized_keys").write_text("earlier\n")
    sandbox_init.install_provisioning(str(provision), str(home), os.getuid(), os.getgid(), sshd_config=str(tmp_path / "cl.conf"))
    assert (home / ".ssh" / "authorized_keys").read_text() == "earlier\n" and not (tmp_path / "cl.conf").exists()


def test_a_service_of_the_sandbox_that_ends_is_started_again_but_not_in_a_tight_loop():
    class Process:
        def __init__(self):
            self.ended = False

        def poll(self):
            return 0 if self.ended else None

    started = []

    def start():
        started.append(Process())
        return started[-1]

    def cannot():
        raise OSError("no such program")
    clock = [100.0]
    running, tried = {}, {}
    services = {"sshd": start, "broken": cannot}
    sandbox_init.keep_running(services, running, tried, pause_s=2, now=lambda: clock[0])
    assert len(started) == 1 and running["broken"] is None
    sandbox_init.keep_running(services, running, tried, pause_s=2, now=lambda: clock[0])
    assert len(started) == 1                      # running: left alone
    started[0].ended = True
    clock[0] += 1
    sandbox_init.keep_running(services, running, tried, pause_s=2, now=lambda: clock[0])
    assert len(started) == 1                      # ended, but tried a second ago
    clock[0] += 2
    sandbox_init.keep_running(services, running, tried, pause_s=2, now=lambda: clock[0])
    assert len(started) == 2 and running["sshd"] is started[1]


# --- the setup recipe, the listed services, cl-expose ----------------------------------------

@pytest.mark.parametrize("state, owed", [("", True), ("running", True), ("none", False), ("done", False), ("failed (exit 3)", False)])
def test_the_recipe_is_owed_once_per_container_and_again_if_it_was_cut_short(state, owed):
    assert sandbox_init.setup_is_owed(state) is owed


def test_the_recipes_state_is_written_and_read_back(tmp_path):
    path = str(tmp_path / "lib" / "setup")
    assert sandbox_init.setup_state(path) == ""
    sandbox_init.write_setup_state("failed (exit 3)", path)
    assert sandbox_init.setup_state(path) == "failed (exit 3)"


def test_a_services_file_lists_one_named_command_a_line():
    text = """
# what should be running
web: python3 -m http.server 8000 --bind 0.0.0.0
  notebook :  jupyter lab --ip 0.0.0.0 --port 8888
tab-8888: cl-expose 8888
web: a second line under a name already taken
Not A Name: echo no
no colon here
empty:
../../etc/passwd: echo no
"""
    services, refused = sandbox_init.parse_services(text)
    assert services == [("web", "python3 -m http.server 8000 --bind 0.0.0.0"), ("notebook", "jupyter lab --ip 0.0.0.0 --port 8888"),
                        ("tab-8888", "cl-expose 8888")]
    assert refused == ["web: a second line under a name already taken", "Not A Name: echo no", "no colon here", "empty:", "../../etc/passwd: echo no"]
    assert sandbox_init.parse_services("") == ([], [])


@pytest.mark.parametrize("breaker", ["\x0c", "\x0b", "\u2028", "\u2029", "\x85", "\x1c", "\r"])
def test_only_a_newline_ends_a_services_line(breaker):
    """Cut at anything else, `rm -rf /workspace/tmp<breaker>/build` would be another command."""
    services, refused = sandbox_init.parse_services(f"clean: rm -rf /workspace/tmp{breaker}/build\nnext: true\r\n")
    assert services == [("clean", f"rm -rf /workspace/tmp{breaker}/build"), ("next", "true")] and refused == []


def test_a_line_with_a_nul_is_no_service():
    """No command can hold one; started, it would end the init, at every start."""
    services, refused = sandbox_init.parse_services("web: printf 'a\0b'\nfine: true\n")
    assert services == [("fine", "true")] and len(refused) == 1


def test_a_services_file_cut_by_the_size_limit_loses_its_cut_line(capsys):
    text = b"a: true\nclean: rm -rf /workspace/tmp/build\n"
    assert sandbox_init.whole_lines(text, 1000) == text                                   # short of the limit: all of it
    assert sandbox_init.whole_lines(text, len(text)) == text                              # exactly the limit: all of it, and not called long
    assert sandbox_init.whole_lines(text[:-1], len(text) - 1) == text[:-1]                # the same without a last newline
    assert "longer than" not in capsys.readouterr().err
    limit = len(b"a: true\nclean: rm -rf /workspace/")
    read = text[:limit + 1]                                                               # what `head` hands over: one byte more
    assert sandbox_init.whole_lines(read, limit) == b"a: true\n"                          # past the limit: the cut line goes
    assert sandbox_init.parse_services(sandbox_init.whole_lines(read, limit).decode()) == ([("a", "true")], [])
    assert sandbox_init.whole_lines(b"a: true\n" + b"x" * 10, 8) == b"a: true\n"          # the limit falls right after a newline
    assert sandbox_init.whole_lines(b"a: true\nb: x\n", 12) == b"a: true\nb: x\n"        # a line ending one byte past it is whole
    assert sandbox_init.whole_lines(b"x" * 11, 10) == b""                                 # one line longer than the limit
    assert "longer than" in capsys.readouterr().err


def test_a_services_file_is_not_followed_past_a_fair_number_of_services():
    many = "".join(f"s{n}: true\n" for n in range(sandbox_init.MAX_SERVICES + 3))
    services, refused = sandbox_init.parse_services(many)
    assert len(services) == sandbox_init.MAX_SERVICES and len(refused) == 3
    started = time.monotonic()
    sandbox_init.parse_services("".join(f"n{n}: true\n" for n in range(20000)))   # a file filled on purpose costs no time
    assert time.monotonic() - started < 2


def test_a_recipes_end_that_cannot_be_recorded_does_not_end_the_init(monkeypatch, tmp_path):
    """A full disk when the recipe ends: the init must go on, with sshd and the files server."""
    class Ended:
        returncode = 0

        def poll(self):
            return 0

    class Running:
        def poll(self):
            return None

    not_a_folder = tmp_path / "file"
    not_a_folder.write_text("")
    monkeypatch.setattr(sandbox_init, "SETUP_STATE", str(not_a_folder / "setup"))
    assert sandbox_init.setup_ended(Ended()) is None          # no exception
    assert sandbox_init.setup_ended(None) is None
    still = Running()
    assert sandbox_init.setup_ended(still) is still
    monkeypatch.setattr(sandbox_init, "SETUP_STATE", str(tmp_path / "setup"))
    failed = Ended()
    failed.returncode = 3
    assert sandbox_init.setup_ended(failed) is None and sandbox_init.setup_state() == "failed (exit 3)"


def archive_of(name, content=b"", link=None, size=None):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, link
            tar.addfile(info)
        else:
            info.size = len(content) if size is None else size
            tar.addfile(info, io.BytesIO(content.ljust(info.size, b"x")))
    return out.getvalue()


@pytest.mark.parametrize("archive, state", [
    (archive_of("setup", b"done\n"), "done"),
    (archive_of("setup", b"running\n"), "running"),
    (archive_of("setup", b"none"), "none"),
    (archive_of("setup", b"failed (exit 3)\n"), "failed (exit 3)"),
    (archive_of("setup", b"failed (exit -9)\n"), "failed (exit -9)"),
    (archive_of("setup", b"<script>alert(1)</script>\n"), ""),      # whatever else the sandbox wrote there
    (archive_of("setup", b"done\nand more\n"), ""),
    (archive_of("setup", b"done", size=5000), ""),                  # too long to be a state
    (archive_of("setup", link="/data/core/keys"), ""),              # a link is not followed, here or by the daemon
    (b"", ""),                                                      # no such file, or no such container
    (b"not a tar archive at all", ""),
])
def test_the_recipes_state_is_one_of_a_few_words_or_nothing(monkeypatch, archive, state):
    monkeypatch.setattr(core, "first_bytes", lambda command, limit, timeout: archive[:limit])
    assert core.setup_state("cl-sb-kafka") == state


def test_only_the_first_bytes_of_a_command_are_read_and_only_for_so_long():
    started = time.monotonic()
    assert core.first_bytes(["yes"], 1000, 20) == b"y\n" * 500          # endless output: cut at the limit
    assert core.first_bytes(["sleep", "30"], 1000, 0.5) == b""          # no output: given up on
    assert core.first_bytes(["printf", "short"], 1000, 20) == b"short"
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("args, ports", [(["8888"], [8888]), (["80", "8080"], [80, 8080]), ([], None), (["0"], None), (["65536"], None),
                                         (["http"], None), (["8888", "-1"], None), (["--serve"], None), (["\u00b2"], None), (["\u0663"], None)])
def test_cl_expose_takes_port_numbers_and_nothing_else(args, ports):
    assert loopback_fwd.ports_of(args) == ports


def told_of(reports, wait_s=5):
    """What cl-expose says, and its exit status, for what its detached relay reported."""
    read, write = os.pipe()
    os.write(write, reports)
    if not reports.endswith(b"\0"):
        os.close(write)          # the relay ended (or closed its report)
    out, err = io.StringIO(), io.StringIO()
    try:
        status, done = loopback_fwd.told(read, wait_s, out, err)
        assert done is (b"done\n" in reports)
        return status, out.getvalue(), err.getvalue()
    finally:
        os.close(read)
        if reports.endswith(b"\0"):
            os.close(write)


def test_cl_expose_says_what_became_of_each_port():
    status, out, err = told_of(b"relaying 8888\nheld 7070\ndone\n")
    assert status == 0 and err == ""
    assert out.splitlines()[0] == "port 8888: now reachable from outside the sandbox"
    assert out.splitlines()[1].startswith("port 7070: already taken on the sandbox's own address, so it is reachable already")
    status, out, err = told_of(b"relaying 8888\nfailed 80 Permission denied\ndone\n")
    assert status == 1 and "port 8888: now reachable" in out and err == "port 80: could not be relayed: Permission denied\n"


def test_a_relay_that_cannot_find_the_sandboxes_address_says_so_for_every_port(monkeypatch):
    import asyncio

    def lost():
        raise OSError("Name or service not known")
    monkeypatch.setattr(loopback_fwd, "own_address", lost)
    reports = []
    asyncio.run(loopback_fwd.relay([8888, 8889], reports.append))
    assert reports == ["failed 8888 the sandbox's own address could not be found (Name or service not known)",
                       "failed 8889 the sandbox's own address could not be found (Name or service not known)", "done"]
    status, out, err = told_of(("\n".join(reports) + "\n").encode())
    assert status == 1 and out == "" and err.count("could not be relayed") == 2


def test_a_port_that_is_taken_is_told_from_one_that_cannot_be_bound(monkeypatch):
    """Taken on the sandbox's address: reachable already, and no failure. Any other reason is one."""
    import asyncio
    import errno

    async def cannot_bind(callback, host, port, reuse_address):
        assert (host, reuse_address) == ("10.210.9.2", False)   # the sandbox's own address, never every address
        raise OSError(errno.EADDRINUSE, "Address already in use") if port == 7070 else OSError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(loopback_fwd, "own_address", lambda: "10.210.9.2")
    monkeypatch.setattr(loopback_fwd.asyncio, "start_server", cannot_bind)
    reports = []
    asyncio.run(loopback_fwd.relay([7070, 80], reports.append))
    assert reports == ["held 7070", "failed 80 Permission denied", "done"]


@pytest.mark.parametrize("report, status, killed", [(b"", 1, True), (b"relaying 8888\n", 1, True), (b"relaying 8888\ndone\n", 0, False),
                                                    (b"relaying 8888\nfailed 80 Permission denied\ndone\n", 1, False)])
def test_a_relay_that_did_not_say_it_was_done_is_stopped_and_one_that_did_is_left(monkeypatch, capsys, report, status, killed):
    """`main` with a stand-in for the detached relay: silent, it is ended, so that "did not
    start" is true; done, it stays, also when one of its ports failed."""
    read, write = os.pipe()
    os.write(write, report)

    class Relay:
        pid = 424242
        stdout = os.fdopen(read, "rb", buffering=0)
    ended = []
    monkeypatch.setattr(loopback_fwd.subprocess, "Popen", lambda *args, **kwargs: Relay)
    monkeypatch.setattr(loopback_fwd.os, "killpg", lambda pid, sig: ended.append((pid, sig)))
    monkeypatch.setattr(loopback_fwd, "REPORT_WAIT_S", 0.3)
    try:
        assert loopback_fwd.main(["8888", "80"]) == status
    finally:
        os.close(write)
        Relay.stdout.close()
    assert ended == ([(424242, loopback_fwd.signal.SIGKILL)] if killed else [])
    assert ("the relay did not start" in capsys.readouterr().err) is killed


def test_cl_expose_fails_when_its_relay_says_nothing_or_dies():
    assert told_of(b"") == (1, "", "cl-expose: the relay did not start\n")                      # died before a word
    status, out, err = told_of(b"relaying 8888\n")                                              # died before it was done
    assert status == 1 and "port 8888" in out and "the relay did not start" in err
    started = time.monotonic()
    status, out, err = told_of(b"relaying 8888\n\0", wait_s=0.5)                                # alive and silent: not waited for long
    assert status == 1 and "port 8888" in out and "the relay did not start" in err and time.monotonic() - started < 5
