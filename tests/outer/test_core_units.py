"""The parts of the core and of the sandbox's own programs that are decided without a daemon."""
import io
import json
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
        "shell_password": "/data/core/keys/kafka/shell_password",
        "ports": "/data/core/ports/kafka.json",
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


def test_a_lock_is_taken_without_waiting_and_a_lock_in_hand_is_not_taken_again(monkeypatch, tmp_path):
    """What the broker relies on: `try_lock` answers at once, and a process that was handed a
    project's lock does the core's work without asking for it a second time (which would wait
    for itself for ever)."""
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    monkeypatch.setattr(core, "HELD", set())
    held = core.try_lock("kafka")
    assert held is not None
    assert core.try_lock("kafka") is None                 # taken: refused at once, not waited for
    other = core.try_lock("other")
    assert other is not None                              # another project is never in the way
    other.close()
    copy = os.dup(held.fileno())                          # as a child gets it
    held.close()
    assert core.try_lock("kafka") is None                 # the copy still holds it
    core.HELD.add("kafka")
    with core.locked("kafka"):                            # in hand: no second lock is asked for
        assert core.try_lock("kafka") is None
    os.close(copy)
    again = core.try_lock("kafka")
    assert again is not None                              # the last copy closed: free
    again.close()
    for bad in ("../x", "", None):
        with pytest.raises(core.CoreError):
            core.try_lock(bad)


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


# --- the web server's settings ---------------------------------------------------------------

@pytest.mark.parametrize("text, port", [("1024", 1024), ("7788", 7788), ("65535", 65535), (" 7788\n", 7788)])
def test_a_port_setting_is_a_port_from_1024_up(text, port):
    assert core.port_setting(text, "CL_WEB_PORT") == port


@pytest.mark.parametrize("text", ["1023", "80", "0", "65536", "", "abc", "7788x", "+7788", "7_788", "77.88", "-7788", "²", "٣٣٣٣"])
def test_any_other_port_setting_is_refused_naming_it(text):
    with pytest.raises(core.CoreError, match=f"^CL_WEB_PORT is {re_quoted(text)}: it must be a port number from 1024 to 65535"):
        core.port_setting(text, "CL_WEB_PORT")


def re_quoted(text):
    import re
    return re.escape(repr(text))


@pytest.mark.parametrize("text, span", [("11100-11164", (11100, 11164)), ("2000-2000", (2000, 2000)), (" 1024-65535 ", (1024, 65535))])
def test_a_range_setting_is_two_ports_low_then_high(text, span):
    assert core.range_setting(text, "CL_ORIGIN_PORTS") == span


@pytest.mark.parametrize("text, why", [("11100", "a range of ports"), ("", "a range of ports"), ("11164-11100", "first port is above its last"),
                                       ("1000-2000", "1024 to 65535"), ("11100-70000", "1024 to 65535"), ("a-b", "1024 to 65535"),
                                       ("11100-", "1024 to 65535"), ("-11100", "1024 to 65535"), ("11100-11164-11200", "1024 to 65535")])
def test_any_other_range_setting_is_refused_naming_it(text, why):
    with pytest.raises(core.CoreError, match="^CL_ORIGIN_PORTS is ") as refused:
        core.range_setting(text, "CL_ORIGIN_PORTS")
    assert why in str(refused.value)


@pytest.mark.parametrize("env, wanted", [({}, True), ({"CL_WEB": "on"}, True), ({"CL_WEB": "ON"}, True), ({"CL_WEB": "off"}, False), ({"CL_WEB": " Off\n"}, False)])
def test_the_web_server_is_on_unless_it_is_turned_off(env, wanted):
    assert core.web_wanted(env) is wanted


@pytest.mark.parametrize("value", ["", "yes", "no", "1", "0", "true", "of"])
def test_any_other_web_switch_is_refused(value):
    with pytest.raises(core.CoreError, match=f"^CL_WEB is {re_quoted(value)}: it must be on or off"):
        core.web_wanted({"CL_WEB": value})


def test_the_web_settings_default_to_the_documented_ports():
    assert core.web_settings({}, 8080) == {"port": 7788, "origin": (11100, 11164), "passthrough": (11200, 11299)}
    given = {"CL_WEB_PORT": "8443", "CL_ORIGIN_PORTS": "20000-20010", "CL_PASSTHROUGH_PORTS": "20011-20100"}
    assert core.web_settings(given, 9000) == {"port": 8443, "origin": (20000, 20010), "passthrough": (20011, 20100)}


@pytest.mark.parametrize("env, named", [
    ({"CL_WEB_PORT": "80"}, "CL_WEB_PORT is '80'"),
    ({"CL_ORIGIN_PORTS": "11100"}, "CL_ORIGIN_PORTS is '11100'"),
    ({"CL_PASSTHROUGH_PORTS": "11299-11200"}, "CL_PASSTHROUGH_PORTS is '11299-11200'"),
])
def test_a_wrong_web_setting_is_refused_naming_it(env, named):
    with pytest.raises(core.CoreError, match=f"^{named}"):
        core.web_settings(env, 8080)


@pytest.mark.parametrize("env, status_port, first, second", [
    ({"CL_WEB_PORT": "11120"}, 8080, "CL_WEB_PORT", "CL_ORIGIN_PORTS"),
    ({"CL_WEB_PORT": "11299"}, 8080, "CL_WEB_PORT", "CL_PASSTHROUGH_PORTS"),
    ({"CL_ORIGIN_PORTS": "11100-11200"}, 8080, "CL_ORIGIN_PORTS", "CL_PASSTHROUGH_PORTS"),
    ({"CL_PASSTHROUGH_PORTS": "11000-11100"}, 8080, "CL_ORIGIN_PORTS", "CL_PASSTHROUGH_PORTS"),
    ({}, 7788, "the status page's port", "CL_WEB_PORT"),
    ({}, 11164, "the status page's port", "CL_ORIGIN_PORTS"),
    ({}, 11200, "the status page's port", "CL_PASSTHROUGH_PORTS"),
])
def test_overlapping_web_settings_are_refused_naming_both(env, status_port, first, second):
    with pytest.raises(core.CoreError, match=f"^{first} and {second} overlap"):
        core.web_settings(env, status_port)


def test_the_web_servers_container_is_created_fenced_and_with_its_two_mounts(monkeypatch):
    monkeypatch.setattr(core, "DATA", "/data")
    args = core.web_args({"port": 7788, "origin": (11100, 11164), "passthrough": (11200, 11299)}, "cl-web:abc")
    # The whole list, so that nothing can be added to it unseen: no capability, no privilege,
    # no restart policy, nothing of a project, the page's port and the two ranges published.
    assert args[:-3] == [
        "--name", "cl-web", "--hostname", "cl-web", "--network", "name=cl-front,gw-priority=1",
        "--label", "cl.role=web",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
        "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "-p", "7788:7788", "-p", "11100-11164:11100-11164", "-p", "11200-11299:11200-11299",
        "--mount", "type=bind,source=/run/cl/broker,target=/run/cl-broker,readonly",
        "--mount", "type=bind,source=/data/web,target=/data",
        "-e", "CL_WEB_PORT=7788", "-e", "CL_LISTEN_SUBNET=10.209.0.0/24",
        "-e", "CL_ORIGIN_PORTS=11100-11164", "-e", "CL_PASSTHROUGH_PORTS=11200-11299",
    ]
    assert args[-3] == "--label" and args[-2].startswith("cl.made-from=") and len(args[-2]) == len("cl.made-from=") + 16
    assert args[-1] == "cl-web:abc"


def test_the_web_servers_label_tells_another_port_or_image():
    settings = {"port": 7788, "origin": (11100, 11164), "passthrough": (11200, 11299)}
    label = core.web_args(settings, "cl-web:abc")[-2]
    assert core.web_args(dict(settings), "cl-web:abc")[-2] == label
    assert core.web_args(dict(settings, port=8443), "cl-web:abc")[-2] != label
    assert core.web_args(settings, "cl-web:def")[-2] != label


def test_the_image_tag_names_the_repository_asked_for(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM x\n")
    sandbox, web = core.image_tag(str(tmp_path)), core.image_tag(str(tmp_path), "cl-web")
    assert sandbox.startswith("cl-sandbox:") and web.startswith("cl-web:")
    assert sandbox.partition(":")[2] == web.partition(":")[2]


def test_the_two_images_are_made_from_their_own_folders():
    assert core.IMAGES == {"sandbox": ("/opt/cl/sandbox", "cl-sandbox", "the sandbox image"),
                           "web": ("/opt/cl/web", "cl-web", "the web server's image")}


# --- a stand-in for the inner daemon --------------------------------------------------------

class Daemon:
    """Answers the docker commands the core gives, from a few sets, and records each."""

    def __init__(self):
        self.calls = []
        self.images = set()
        self.preloads = {}             # a preload file: the tags it holds
        self.build_fails = False
        self.networks = {}             # name: id
        self.containers = {}           # name: {"labels": {...}, "running": bool}
        self.connect_answers = []      # (code, stderr) for the next `network connect`s
        self.rm_network_answers = []   # (code, stderr) for the next `network rm`s
        self.silent = False

    def __call__(self, *args, timeout=120, check=True):
        self.calls.append(args)
        done = self.answer(args)
        if check and done.returncode != 0:
            raise core.CoreError(f"`docker {' '.join(args[:2])}` failed: {done.stderr}")
        return done

    def answer(self, args):
        import json
        if self.silent:
            return answer(1, "Cannot connect to the Docker daemon")
        if args[:2] == ("image", "inspect"):
            return answer(0, stdout="[{}]") if args[2] in self.images else answer(1, f"Error response from daemon: No such image: {args[2]}")
        if args[:2] == ("network", "inspect"):
            name = args[-1]
            if name not in self.networks:
                return answer(1, f"Error response from daemon: network {name} not found")
            if "-f" in args:
                attached = [c for c, one in self.containers.items() if name in one.get("networks", ())]
                return answer(0, stdout=" ".join(attached) + " ")
            return answer(0, stdout=json.dumps([{"Id": self.networks[name]}]))
        if args[:2] == ("container", "inspect"):
            one = self.containers.get(args[2])
            if one is None:
                return answer(1, f"Error response from daemon: No such container: {args[2]}")
            return answer(0, stdout=json.dumps([{"Config": {"Labels": one["labels"]}, "State": {"Running": one["running"]},
                                                 "NetworkSettings": {"Networks": {}}}]))
        if args[0] == "load":
            self.images |= self.preloads.get(args[2], set())
            return answer(0)
        if args[0] == "build":
            if self.build_fails:
                return answer(1, "failed to fetch the base image")
            self.images.add(args[args.index("-t") + 1])
            return answer(0)
        if args[:2] == ("network", "create"):
            self.networks[args[-1]] = f"{len(self.networks):x}".rjust(64, "f")
            return answer(0)
        if args[:2] == ("network", "connect"):
            code, stderr = self.connect_answers.pop(0) if self.connect_answers else (0, "")
            if code == 0:
                self.containers[args[3]].setdefault("networks", []).append(args[2])
            return answer(code, stderr)
        if args[:2] == ("network", "disconnect"):
            return answer(0)
        if args[:2] == ("network", "rm"):
            if args[2] not in self.networks:
                return answer(1, f"Error response from daemon: network {args[2]} not found")
            code, stderr = self.rm_network_answers.pop(0) if self.rm_network_answers else (0, "")
            if code == 0:
                self.networks.pop(args[2])
            return answer(code, stderr)
        if args[0] == "create":
            labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
            self.containers[args[args.index("--name") + 1]] = {"labels": labels, "running": False}
            return answer(0)
        if args[0] == "rm":
            self.containers.pop(args[-1], None)
            return answer(0)
        if args[0] == "start":
            self.containers[args[1]]["running"] = True
            return answer(0)
        return answer(0)

    def made(self):
        """What was made, removed, connected or started, in order: everything but questions."""
        return [c for c in self.calls if "inspect" not in c[:2]]


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    one = Daemon()
    monkeypatch.setattr(core, "docker", one)
    monkeypatch.setattr(core, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    monkeypatch.setattr(core, "PRELOAD", str(tmp_path / "preload"))
    for kind, repo, called in (("sandbox", "cl-sandbox", "the sandbox image"), ("web", "cl-web", "the web server's image")):
        (tmp_path / kind).mkdir()
        (tmp_path / kind / "Dockerfile").write_text(f"FROM {kind}\n")
        monkeypatch.setitem(core.IMAGES, kind, (str(tmp_path / kind), repo, called))
    (tmp_path / "preload").mkdir()
    return one


@pytest.mark.parametrize("kind", ["sandbox", "web"])
def test_an_image_that_is_there_is_taken_as_it_is(daemon, kind):
    folder, repo, _ = core.IMAGES[kind]
    tag = core.image_tag(folder, repo)
    daemon.images.add(tag)
    assert core.ensure_image(kind=kind) == tag
    assert daemon.made() == []


@pytest.mark.parametrize("kind", ["sandbox", "web"])
def test_an_image_without_a_preload_is_built(daemon, kind):
    folder, repo, called = core.IMAGES[kind]
    said = []
    tag = core.ensure_image(said.append, kind)
    assert tag == core.image_tag(folder, repo) and tag.startswith(repo + ":")
    assert daemon.made() == [("network", "create", "cl-build"), ("build", "--network", "cl-build", "-t", tag, folder)]
    assert said == [f"building {called} (once; this takes a few minutes and needs the internet)", f"{called} is built"]


@pytest.mark.parametrize("kind", ["sandbox", "web"])
def test_an_image_in_the_preload_folder_is_loaded_and_not_built(daemon, kind):
    folder, repo, _ = core.IMAGES[kind]
    tag = core.image_tag(folder, repo)
    preload = f"{core.PRELOAD}/{repo}.tar"
    open(preload, "w").close()
    daemon.preloads[preload] = {tag}
    said = []
    assert core.ensure_image(said.append, kind) == tag
    assert daemon.made() == [("load", "-i", preload)] and said == []


@pytest.mark.parametrize("kind", ["sandbox", "web"])
def test_a_preload_that_holds_another_image_is_said_and_the_image_built(daemon, kind):
    folder, repo, _ = core.IMAGES[kind]
    tag = core.image_tag(folder, repo)
    preload = f"{core.PRELOAD}/{repo}.tar"
    open(preload, "w").close()
    daemon.preloads[preload] = {f"{repo}:older"}
    said = []
    assert core.ensure_image(said.append, kind) == tag
    assert [c[0] for c in daemon.made()] == ["load", "network", "build"]
    assert said[0] == f"{preload} does not hold {tag}; building it"


@pytest.mark.parametrize("kind, wording", [("sandbox", "The sandbox image could not be built: "), ("web", "The web server's image could not be built: ")])
def test_an_image_that_cannot_be_built_is_an_image_error(daemon, kind, wording):
    daemon.build_fails = True
    with pytest.raises(core.ImageError) as failed:
        core.ensure_image(kind=kind)
    assert str(failed.value) == wording + "failed to fetch the base image"
    assert isinstance(failed.value, core.CoreError)


# --- the front network's routes ----------------------------------------------------------------

ROUTES = (
    "default via 172.17.0.1 dev eth0\n"
    "172.17.0.0/16 dev eth0 proto kernel scope link src 172.17.0.2\n"
    "10.210.0.0/24 dev br-0a1b2c3d4e5f proto kernel scope link src 10.210.0.1\n"
)
FRONT_ROUTE = "10.209.0.0/24 dev br-c0479f62d595 proto kernel scope link src 10.209.0.1 linkdown"


def test_routes_that_do_not_overlap_the_front_network_are_no_clash():
    assert core.overlapping_route("10.209.0.0/24", ROUTES) == ""
    assert core.overlapping_route("10.209.0.0/24", "") == ""
    assert core.overlapping_route("10.209.0.0/24", "\n  \n" + ROUTES) == ""                    # a blank line is no route


@pytest.mark.parametrize("route", ["10.209.0.0/24 dev wg0 scope link", "10.0.0.0/8 dev tun0 scope link", "10.209.0.128/25 dev eth1",
                                   "10.209.0.7 via 172.17.0.1 dev eth0", FRONT_ROUTE])
def test_a_route_that_overlaps_the_front_network_is_named(route):
    assert core.overlapping_route("10.209.0.0/24", ROUTES + route + "\n") == route


def test_the_default_route_is_no_clash():
    assert core.overlapping_route("10.209.0.0/24", "default via 10.209.0.1 dev eth0\n") == ""


def test_the_front_networks_own_bridge_is_left_out():
    assert core.overlapping_route("10.209.0.0/24", ROUTES + FRONT_ROUTE + "\n", own_device="br-c0479f62d595") == ""
    assert core.overlapping_route("10.209.0.0/24", ROUTES + FRONT_ROUTE + "\n", own_device="br-000000000000") == FRONT_ROUTE
    other = "10.209.0.0/16 dev wg0 scope link"
    assert core.overlapping_route("10.209.0.0/24", FRONT_ROUTE + "\n" + other + "\n", own_device="br-c0479f62d595") == other


def test_routes_that_cannot_be_read_are_an_error(monkeypatch):
    monkeypatch.setattr(core.subprocess, "run", lambda *args, **kwargs: answer(0, stdout=ROUTES))
    assert core.read_routes() == ROUTES
    monkeypatch.setattr(core.subprocess, "run", lambda *args, **kwargs: answer(1, "Cannot open netlink socket"))
    with pytest.raises(core.CoreError, match="routes could not be read"):
        core.read_routes()

    def hangs(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(core.subprocess, "run", hangs)
    with pytest.raises(core.CoreError, match="routes could not be read"):
        core.read_routes()

    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'ip'")
    monkeypatch.setattr(core.subprocess, "run", missing)
    with pytest.raises(core.CoreError, match="routes could not be read"):
        core.read_routes()


# --- bringing the web server up --------------------------------------------------------------

SETTINGS = {"port": 7788, "origin": (11100, 11164), "passthrough": (11200, 11299)}


@pytest.fixture
def web_daemon(daemon, monkeypatch):
    """The daemon with the web image there, three projects recorded (b's network is gone), and
    the routes of ROUTES. Questions and answers in one list: ("image",), ("routes",), docker."""
    monkeypatch.setattr(core, "ensure_image", lambda say=core.say_nothing, kind="sandbox": daemon.calls.append(("image", kind)) or "cl-web:abc")
    daemon.routes = ROUTES
    monkeypatch.setattr(core, "read_routes", lambda: daemon.calls.append(("routes",)) or daemon.routes)
    for name in ("a", "b", "c"):
        core.write_record(core.new_record(name))
    daemon.networks.update({"cl-net-a": "a" * 64, "cl-net-c": "c" * 64})
    return daemon


def test_a_first_web_up_makes_the_network_and_the_container_then_connects_and_starts_it(web_daemon):
    core.web_up(SETTINGS)
    args = core.web_args(SETTINGS, "cl-web:abc")
    assert web_daemon.made() == [
        ("image", "web"), ("routes",),
        ("network", "create", "--subnet", "10.209.0.0/24", "--label", "cl.role=front", "cl-front"),
        ("create", *args),
        ("network", "connect", "cl-net-a", "cl-web"), ("network", "connect", "cl-net-c", "cl-web"),
        ("start", "cl-web"),
    ]
    assert os.path.isdir(f"{core.DATA}/web")


def test_a_web_server_made_from_the_same_image_and_settings_is_kept(web_daemon):
    core.web_up(SETTINGS)
    web_daemon.calls.clear()
    web_daemon.containers["cl-web"]["running"] = False
    said = []
    core.web_up(SETTINGS, said.append)
    assert web_daemon.made() == [("image", "web"), ("routes",), ("network", "connect", "cl-net-a", "cl-web"),
                                 ("network", "connect", "cl-net-c", "cl-web"), ("start", "cl-web")]
    assert said == []


def test_a_web_server_made_otherwise_is_made_again(web_daemon):
    core.web_up(dict(SETTINGS, port=8443))
    web_daemon.calls.clear()
    said = []
    core.web_up(SETTINGS, said.append)
    made = web_daemon.made()
    assert made[2][:2] == ("rm", "-f") and made[3] == ("create", *core.web_args(SETTINGS, "cl-web:abc")) and made[-1] == ("start", "cl-web")
    assert said == ["the web server's container was made from another image or other settings; making it again"]


def test_the_routes_are_checked_at_every_web_up_leaving_out_the_front_bridge(web_daemon):
    web_daemon.networks["cl-front"] = "c0479f62d595" + "0" * 52
    web_daemon.routes = ROUTES + FRONT_ROUTE + "\n"
    core.web_up(SETTINGS)                                     # its own route: no clash, and no second network
    assert ("routes",) in web_daemon.calls and not [c for c in web_daemon.made() if c[:2] == ("network", "create")]
    web_daemon.calls.clear()
    web_daemon.routes += "10.209.0.0/16 dev wg0 scope link\n"  # a route that came since
    with pytest.raises(core.CoreError, match=r"10\.209\.0\.0/24 overlaps a network this container is on \(10\.209\.0\.0/16 dev wg0"):
        core.web_up(SETTINGS)
    assert web_daemon.made() == [("image", "web"), ("routes",)]


def test_an_overlap_stops_a_web_up_before_anything_is_made(web_daemon):
    web_daemon.routes = ROUTES + "10.209.0.0/24 dev wg0 scope link\n"
    with pytest.raises(core.CoreError, match="overlaps"):
        core.web_up(SETTINGS)
    assert web_daemon.made() == [("image", "web"), ("routes",)]
    web_daemon.networks["cl-front"] = "d" * 64                 # there, on another bridge than the route's
    web_daemon.routes = ROUTES + FRONT_ROUTE + "\n"
    web_daemon.calls.clear()
    with pytest.raises(core.CoreError, match="overlaps"):
        core.web_up(SETTINGS)
    assert web_daemon.made() == [("image", "web"), ("routes",)]


def test_unreadable_routes_stop_a_web_up_before_anything_is_made(web_daemon, monkeypatch):
    def unreadable():
        web_daemon.calls.append(("routes",))
        raise core.CoreError("This container's routes could not be read (no ip)")
    monkeypatch.setattr(core, "read_routes", unreadable)
    with pytest.raises(core.CoreError, match="routes could not be read"):
        core.web_up(SETTINGS)
    assert web_daemon.made() == [("image", "web"), ("routes",)]


def test_no_web_server_means_nothing_to_connect(daemon):
    said = []
    core.connect_web("cl-net-a", said.append)
    assert daemon.made() == [] and said == []


@pytest.mark.parametrize("code, stderr, told", [
    (0, "", False),
    (1, "Error response from daemon: endpoint with name cl-web already exists in network cl-net-a", False),
    (1, "Error response from daemon: network cl-net-a not found", True),
])
def test_connecting_the_web_server_says_a_failure_and_never_raises(daemon, code, stderr, told):
    daemon.containers["cl-web"] = {"labels": {}, "running": True}
    daemon.connect_answers.append((code, stderr))
    said = []
    core.connect_web("cl-net-a", said.append)
    assert daemon.made() == [("network", "connect", "cl-net-a", "cl-web")]
    assert said == ([f"the web server could not be connected to cl-net-a: {stderr}"] if told else [])


def test_a_silent_daemon_when_connecting_the_web_server_is_said_not_raised(daemon):
    daemon.silent = True
    said = []
    core.connect_web("cl-net-a", said.append)
    assert len(said) == 1 and said[0].startswith("the web server could not be connected to cl-net-a: The inner Docker daemon could not say")


def test_ensure_connects_the_web_server_after_the_network_and_before_the_sandbox(daemon, monkeypatch):
    monkeypatch.setattr(core, "SANDBOX_UID", os.getuid())
    monkeypatch.setattr(core, "write_provisioning", lambda name: None)
    monkeypatch.setattr(core, "ensure_image", lambda say=core.say_nothing, kind="sandbox": "cl-sandbox:abc")
    daemon.containers["cl-web"] = {"labels": {}, "running": True}
    record = core.new_record("kafka")
    core.ensure(record)
    assert [c[:3] for c in daemon.made()] == [("network", "create", "--label"), ("network", "connect", "cl-net-kafka"), ("create", "--name", "cl-sb-kafka")]


@pytest.mark.parametrize("first, second, problem", [((1, "has active endpoints"), (0, ""), False),
                                                    ((1, "has active endpoints"), (1, "still has active endpoints"), True)])
def test_a_projects_network_is_removed_at_the_second_try_and_a_problem_only_when_both_failed(daemon, first, second, problem):
    core.write_record(core.new_record("kafka"))
    daemon.networks["cl-net-kafka"] = "e" * 64
    daemon.containers["cl-web"] = {"labels": {}, "running": True, "networks": ["cl-net-kafka"]}
    daemon.rm_network_answers += [first, second]
    if problem:
        with pytest.raises(core.CoreError, match=r"the network \(still has active endpoints\)"):
            core.remove_everything("kafka")
    else:
        core.remove_everything("kafka")
        assert "cl-net-kafka" not in daemon.networks
    removals = [c for c in daemon.made() if c[:2] == ("network", "rm")]
    disconnects = [c for c in daemon.made() if c[:2] == ("network", "disconnect")]
    assert len(removals) == 2 and disconnects == [("network", "disconnect", "-f", "cl-net-kafka", "cl-web")] * 2


def test_a_projects_network_removed_at_once_is_not_tried_again(daemon):
    core.write_record(core.new_record("kafka"))
    daemon.networks["cl-net-kafka"] = "e" * 64
    core.remove_everything("kafka")
    assert [c for c in daemon.made() if c[:2] == ("network", "rm")] == [("network", "rm", "cl-net-kafka")]


# --- how the web server stands -------------------------------------------------------------------

def web_container(running=True, address="10.209.0.2"):
    networks = {"cl-front": {"IPAddress": address}} if address is not None else {}
    return {"State": {"Running": running}, "NetworkSettings": {"Networks": networks}}


class Answer:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_web_server_that_answers_is_up(monkeypatch):
    asked = []
    monkeypatch.setattr(core, "inspect_container", lambda name: web_container())
    monkeypatch.setattr(core.urllib.request, "urlopen", lambda url, timeout, context: asked.append(url) or Answer(200))
    assert core.web_health(SETTINGS) == ("up", "")
    assert asked == ["https://10.209.0.2:7788/"]


def test_no_web_server_container_is_absent(monkeypatch):
    monkeypatch.setattr(core, "inspect_container", lambda name: None)
    assert core.web_health(SETTINGS) == ("absent", "there is no web server container")


def test_a_web_server_that_is_not_running_ended_with_the_last_line_of_its_log(monkeypatch):
    monkeypatch.setattr(core, "inspect_container", lambda name: web_container(running=False))
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(0, "OSError: [Errno 99] Cannot assign requested address\n"))
    assert core.web_health(SETTINGS) == ("ended", "the web server ended: OSError: [Errno 99] Cannot assign requested address")
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(0))
    assert core.web_health(SETTINGS) == ("ended", "the web server ended")


@pytest.mark.parametrize("problem", [core.urllib.error.URLError("Connection refused"), TimeoutError("timed out"), ConnectionResetError(104, "reset")])
def test_a_running_web_server_that_does_not_answer_is_silent(monkeypatch, problem):
    def fails(url, timeout, context):
        raise problem
    monkeypatch.setattr(core, "inspect_container", lambda name: web_container())
    monkeypatch.setattr(core.urllib.request, "urlopen", fails)
    kind, text = core.web_health(SETTINGS)
    assert kind == "silent" and text.startswith("the web server does not answer yet (")


def test_a_running_web_server_without_a_front_address_or_a_good_answer_is_silent(monkeypatch):
    monkeypatch.setattr(core.urllib.request, "urlopen", lambda url, timeout, context: Answer(204))
    for container in (web_container(address=None), web_container(address="")):
        monkeypatch.setattr(core, "inspect_container", lambda name, c=container: c)
        assert core.web_health(SETTINGS) == ("silent", "the web server does not answer yet (it has no address on cl-front)")
    monkeypatch.setattr(core, "inspect_container", lambda name: web_container())
    assert core.web_health(SETTINGS) == ("silent", "the web server does not answer yet (it answered 204)")


def test_a_daemon_that_cannot_be_asked_about_the_web_server_is_unknown(monkeypatch):
    def silent(name):
        raise core.CoreError("The inner Docker daemon could not say whether cl-web exists: Cannot connect")
    monkeypatch.setattr(core, "inspect_container", silent)
    assert core.web_health(SETTINGS) == ("unknown", "The inner Docker daemon could not say whether cl-web exists: Cannot connect")
    monkeypatch.setattr(core, "inspect_container", lambda name: web_container(running=False))

    def logs_hang(*args, **kwargs):
        raise core.CoreError("The inner Docker daemon did not answer `docker logs`.")
    monkeypatch.setattr(core, "docker", logs_hang)
    assert core.web_health(SETTINGS) == ("unknown", "The inner Docker daemon did not answer `docker logs`.")


# --- keeping the web server up -------------------------------------------------------------------

ENDED = ("ended", "the web server ended: OSError: boom")
SILENT = ("silent", "the web server does not answer yet (Connection refused)")
UP = ("up", "")
ABSENT = ("absent", "there is no web server container")


def keep_web_with(monkeypatch, healths, ups=(), sandbox_image=None, env=None, turns=None):
    """keep_web with stand-ins: `healths` is what web_health says turn by turn; `ups` what each
    web_up does (None: works; a string: says it and works; an exception: raised); the clock
    moves by what it sleeps. Returns (exit, reports, sleeps, web_up calls, sandbox image calls)."""
    healths, ups = list(healths), list(ups)
    reports, sleeps, called, images, clock = [], [], [], [], [0.0]

    def web_up(settings, say):
        called.append(settings)
        outcome = ups.pop(0) if ups else None
        if isinstance(outcome, Exception):
            raise outcome
        if outcome:
            say(outcome)

    def ensure_image(say=core.say_nothing, kind="sandbox"):
        images.append(kind)
        say("building the sandbox image (once; this takes a few minutes and needs the internet)")
        if sandbox_image is not None:
            raise sandbox_image
        return "cl-sandbox:abc"

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(core, "web_health", lambda settings: healths.pop(0))
    monkeypatch.setattr(core, "web_up", web_up)
    monkeypatch.setattr(core, "ensure_image", ensure_image)
    code = core.keep_web(env or {}, 8080, lambda key, value: reports.append((key, value)), sleep=sleep,
                         turns=len(healths) if turns is None else turns, now=lambda: clock[0])
    return code, reports, sleeps, called, images


def test_a_first_start_is_preparing_then_up_then_the_sandbox_image_is_ready(monkeypatch):
    code, reports, sleeps, called, images = keep_web_with(monkeypatch, [ABSENT, SILENT, UP, UP, UP], ["building the web server's image"])
    assert code == 0
    assert reports == [("web", "preparing"), ("web", "preparing: building the web server's image"), ("web", "up"),
                       ("sandbox_image", "preparing: building the sandbox image (once; this takes a few minutes and needs the internet)"),
                       ("sandbox_image", "ready")]
    assert sleeps == [1, 1, 5, 5, 5] and len(called) == 1 and images == ["sandbox"]
    assert called[0] == {"port": 7788, "origin": (11100, 11164), "passthrough": (11200, 11299)}


def test_a_web_server_left_from_a_start_that_ended_is_a_start_too(monkeypatch):
    _, reports, sleeps, called, _ = keep_web_with(monkeypatch, [ENDED, UP])
    assert reports[:2] == [("web", "preparing"), ("web", "up")] and len(called) == 1 and sleeps[0] == 1


def test_an_unchanged_state_is_reported_once(monkeypatch):
    _, reports, _, _, _ = keep_web_with(monkeypatch, [ABSENT] + [UP] * 6)
    assert [r for r in reports if r[0] == "web"] == [("web", "preparing"), ("web", "up")]


def test_with_the_web_server_off_one_left_by_an_earlier_start_is_removed_and_nothing_is_built(monkeypatch, daemon):
    daemon.containers["cl-web"] = {"labels": {}, "running": True}
    code, reports, sleeps, called, images = keep_web_with(monkeypatch, [], env={"CL_WEB": "off"}, turns=5)
    assert code == 0 and reports == [("web", "off")] and called == [] and images == [] and sleeps == []
    assert daemon.made() == [("rm", "-f", "-v", "cl-web")] and "cl-web" not in daemon.containers
    daemon.calls.clear()
    assert keep_web_with(monkeypatch, [], env={"CL_WEB": "off"}, turns=5)[:2] == (0, [("web", "off")])
    assert daemon.made() == []                                            # none there: nothing removed


def test_with_the_web_server_off_and_a_silent_daemon_it_is_down(monkeypatch, daemon):
    daemon.silent = True
    code, reports, _, _, _ = keep_web_with(monkeypatch, [], env={"CL_WEB": "off"}, turns=5)
    assert code == 3 and len(reports) == 1 and reports[0][1].startswith("down: The inner Docker daemon could not say whether cl-web exists")


def test_a_wrong_web_switch_is_down_with_its_reason(monkeypatch):
    code, reports, sleeps, called, _ = keep_web_with(monkeypatch, [UP], env={"CL_WEB": "maybe"}, turns=5)
    assert code == 3 and reports == [("web", "down: CL_WEB is 'maybe': it must be on or off.")] and called == [] and sleeps == []


def test_a_wrong_web_setting_is_down_with_its_reason(monkeypatch):
    code, reports, _, called, _ = keep_web_with(monkeypatch, [UP], env={"CL_WEB_PORT": "8080"}, turns=5)
    assert code == 3 and called == []
    assert reports == [("web", "down: the status page's port and CL_WEB_PORT overlap (8080-8080 and 8080-8080).")]


def test_an_image_that_cannot_be_had_is_down_for_this_run(monkeypatch):
    failed = core.ImageError("The web server's image could not be built: no network")
    code, reports, _, called, images = keep_web_with(monkeypatch, [ABSENT] * 5, [failed])
    assert code == 3 and len(called) == 1 and images == []
    assert reports == [("web", "preparing"), ("web", "down: The web server's image could not be built: no network")]


def test_a_web_server_that_ends_at_once_is_down_with_its_reason_and_started_again_each_turn(monkeypatch):
    code, reports, sleeps, called, _ = keep_web_with(monkeypatch, [ABSENT, ENDED, ENDED, ENDED, ENDED])
    assert code == 0
    assert reports == [("web", "preparing"), ("web", "down: the web server ended: OSError: boom")]
    assert len(called) == 5 and sleeps == [1, 5, 5, 5, 5]


def test_a_web_server_that_ends_after_being_up_is_down_then_up_again(monkeypatch):
    killed = ("ended", "the web server ended: Killed")
    _, reports, sleeps, called, images = keep_web_with(monkeypatch, [ABSENT, UP, UP, killed, SILENT, UP])
    assert [r for r in reports if r[0] == "web"] == [("web", "preparing"), ("web", "up"), ("web", "down: the web server ended: Killed"), ("web", "up")]
    assert len(called) == 2 and images == ["sandbox"] and sleeps[3:] == [5, 5, 5]


def test_a_web_server_gone_later_is_down_and_made_again(monkeypatch):
    _, reports, _, called, _ = keep_web_with(monkeypatch, [ABSENT, UP, ABSENT, UP])
    assert [r for r in reports if r[0] == "web"] == [("web", "preparing"), ("web", "up"), ("web", "down: there is no web server container"), ("web", "up")]
    assert len(called) == 2


def test_a_daemon_that_cannot_be_asked_is_down_even_at_the_first_turn(monkeypatch):
    unknown = ("unknown", "The inner Docker daemon could not say whether cl-web exists")
    _, reports, _, called, _ = keep_web_with(monkeypatch, [unknown, UP])
    assert [r for r in reports if r[0] == "web"] == [("web", "preparing"), ("web", "down: " + unknown[1]), ("web", "up")]
    assert len(called) == 1


def test_a_web_up_that_fails_otherwise_is_down_and_tried_again(monkeypatch):
    """Tried again every turn; the state keeps the failure's reason, said once, and does not
    take turns with what web_health says until a web_up works."""
    overlap = core.CoreError("The web server's network 10.209.0.0/24 overlaps a network this container is on (x).")
    code, reports, sleeps, called, _ = keep_web_with(monkeypatch, [ABSENT, ABSENT, ABSENT, ABSENT, ENDED, UP], [overlap, overlap, overlap])
    assert code == 0 and len(called) == 5
    assert [r for r in reports if r[0] == "web"] == [("web", "preparing"), ("web", "down: " + str(overlap)),
                                                     ("web", "down: " + ENDED[1]), ("web", "up")]
    assert sleeps[:5] == [5, 5, 5, 5, 5]


def test_a_web_up_that_fails_with_an_os_error_is_down_and_tried_again(monkeypatch):
    """Its folder on the data volume cannot be made, say: down with the reason, and the keeper
    goes on instead of ending with a traceback."""
    denied = PermissionError(13, "Permission denied", "/data/web")
    code, reports, sleeps, called, _ = keep_web_with(monkeypatch, [ABSENT, ABSENT, ABSENT, UP], [denied, denied])
    assert code == 0 and len(called) == 3
    assert [r for r in reports if r[0] == "web"] == [("web", "preparing"), ("web", "down: " + str(denied)), ("web", "up")]
    assert sleeps[:3] == [5, 5, 5]


def test_a_web_server_that_stays_silent_is_down_after_thirty_seconds_and_not_restarted(monkeypatch):
    # Silent from the third turn on, five seconds apart: the eighth silent turn is 35 s on.
    _, reports, sleeps, called, _ = keep_web_with(monkeypatch, [ABSENT, UP] + [SILENT] * 10)
    web = [r for r in reports if r[0] == "web"]
    assert web == [("web", "preparing"), ("web", "up"), ("web", "down: " + SILENT[1])]
    assert len(called) == 1 and sleeps[2:] == [5] * 10
    _, reports, _, _, _ = keep_web_with(monkeypatch, [ABSENT, UP] + [SILENT] * 8)        # 35 s: down
    assert [r for r in reports if r[0] == "web"][-1] == ("web", "down: " + SILENT[1])
    _, reports, _, _, _ = keep_web_with(monkeypatch, [ABSENT, UP] + [SILENT] * 7)        # 30 s silent, not more: still up
    assert [r for r in reports if r[0] == "web"][-1] == ("web", "up")


def test_the_silence_is_counted_afresh_after_an_answer(monkeypatch):
    _, reports, _, called, _ = keep_web_with(monkeypatch, [ABSENT, UP] + [SILENT] * 6 + [UP] + [SILENT] * 6)
    assert [r for r in reports if r[0] == "web"] == [("web", "preparing"), ("web", "up")] and len(called) == 1


def test_a_state_said_after_a_progress_line_is_said_again(monkeypatch):
    """A progress line stands in the state until the next one: the same "down" after it is
    news again."""
    _, reports, _, _, _ = keep_web_with(monkeypatch, [ABSENT, ENDED, ENDED, ENDED], [None, "making it again"])
    assert [r for r in reports if r[0] == "web"] == [("web", "preparing"), ("web", "down: " + ENDED[1]), ("web", "preparing: making it again"),
                                                     ("web", "down: " + ENDED[1])]


def test_the_web_keep_verb_prints_one_json_object_a_line_and_passes_the_exit_on(monkeypatch, capsys):
    seen = []

    def keep_web(env, status_port, report):
        seen.append((env is os.environ, status_port))
        report("web", "preparing")
        report("sandbox_image", "ready")
        return 3
    monkeypatch.setattr(core, "keep_web", keep_web)
    assert core.main(["web-keep", "9191"]) == 3
    assert capsys.readouterr().out == '{"web": "preparing"}\n{"sandbox_image": "ready"}\n'
    assert seen == [(True, 9191)]


def test_a_web_server_that_never_answers_does_not_stay_preparing(monkeypatch):
    _, reports, sleeps, called, _ = keep_web_with(monkeypatch, [ABSENT] + [SILENT] * 40)
    assert reports[-1] == ("web", "down: " + SILENT[1]) and len(called) == 1
    assert sleeps.count(1) == 32                                         # one-second turns while preparing, then five


def test_a_sandbox_image_that_fails_leaves_the_web_server_up_and_is_not_tried_again(monkeypatch):
    failed = core.ImageError("The sandbox image could not be built: no network")
    _, reports, _, _, images = keep_web_with(monkeypatch, [ABSENT, UP, UP, UP], sandbox_image=failed)
    assert reports[-1] == ("sandbox_image", "failed: The sandbox image could not be built: no network")
    assert [r for r in reports if r[0] == "web"][-1] == ("web", "up") and images == ["sandbox"]


# --- the Shell's password and login ----------------------------------------------------------

HOST_PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFuQY96dMIi969HDZQuBi0c3I/01XhZ9te8rSEQfKPZD cl-sb-kafka\n"
HOST_FINGERPRINT = "SHA256:R0lwxQigpMClG95RC9zlQ/nOXa23/2N1ZIPc6EJpBAw"   # what `ssh-keygen -lf` says of HOST_PUBLIC
A_HASH = "$6$saltsalt$" + "a" * 86


def test_the_shell_password_is_made_once_kept_and_handed_over_as_its_hash(monkeypatch, tmp_path):
    """Real ssh-keygen and openssl: the password is 32 letters and digits, 0600 with the
    core's keys, the same after a second provisioning; the provisioning folder holds its
    hash (which openssl gives back for the same salt) and not the password."""
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    core.write_provisioning("kafka")
    p = core.paths("kafka")
    password = core.shell_password("kafka")
    assert len(password) == 32 and password.isalnum() and os.path.dirname(p["shell_password"]) == p["host_keys"]
    assert oct(os.stat(p["shell_password"]).st_mode & 0o777) == "0o600"
    core.write_provisioning("kafka")
    assert core.shell_password("kafka") == password
    hashed = (tmp_path / "core" / "provision" / "kafka" / "shell_password_hash").read_text().strip()
    assert core.CRYPT_HASH.fullmatch(hashed) and oct(os.stat(f"{p['provision']}/shell_password_hash").st_mode & 0o777) == "0o600"
    salt = hashed.split("$")[2]
    again = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input=password + "\n", capture_output=True, text=True, check=True)
    assert again.stdout.strip() == hashed
    assert not any(password in f.read_text(errors="replace") for f in tmp_path.rglob("*") if f.is_file() and str(f) != p["shell_password"])
    assert "PasswordAuthentication yes" in (tmp_path / "core" / "provision" / "kafka" / "sshd_config").read_text()


def stand_in_openssl(tmp_path, monkeypatch, prints, status=0):
    """An `openssl` first on the PATH that writes down its arguments and standard input."""
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "openssl"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/args"\ncat > "{tmp_path}/stdin"\necho \'{prints}\'\nexit {status}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}:{os.environ['PATH']}")


def test_openssl_gets_the_password_on_standard_input_and_never_in_its_arguments(monkeypatch, tmp_path):
    stand_in_openssl(tmp_path, monkeypatch, A_HASH)
    assert core.crypt_hash("TheShellPassword") == A_HASH
    assert (tmp_path / "args").read_text() == "passwd\n-6\n-stdin\n"
    assert (tmp_path / "stdin").read_text() == "TheShellPassword\n"


def test_an_openssl_that_fails_is_an_error_that_does_not_repeat_the_password(monkeypatch, tmp_path):
    stand_in_openssl(tmp_path, monkeypatch, "TheShellPassword", status=1)
    with pytest.raises(core.CoreError) as refused:
        core.crypt_hash("TheShellPassword")
    assert "TheShellPassword" not in str(refused.value)


def test_the_host_keys_fingerprint_is_the_one_ssh_keygen_gives(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    keys = tmp_path / "core" / "keys" / "kafka"
    keys.mkdir(parents=True)
    (keys / "ssh_host_ed25519_key.pub").write_text(HOST_PUBLIC)
    assert core.host_fingerprint("kafka") == HOST_FINGERPRINT


def sandbox_of(running, address="172.18.0.2", pid=4242):
    return {"Id": "c" * 64, "State": {"Running": running, "Pid": pid if running else 0},
            "NetworkSettings": {"Networks": {"cl-net-kafka": {"IPAddress": address}}}}


@pytest.fixture
def project(monkeypatch, tmp_path):
    """A recorded project kafka with its host key and Shell password, and a sandbox the test says is running or not."""
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    core.write_record(core.new_record("kafka"))
    keys = tmp_path / "core" / "keys" / "kafka"
    keys.mkdir(parents=True)
    (keys / "ssh_host_ed25519_key.pub").write_text(HOST_PUBLIC)
    (keys / "shell_password").write_text("TheShellPassword\n")
    sandbox = {"now": sandbox_of(True)}
    monkeypatch.setattr(core, "inspect_container", lambda name: sandbox["now"] if name == "cl-sb-kafka" else None)
    return sandbox


def test_attach_gives_a_running_sandboxs_login_and_refuses_any_other(project):
    assert core.attach("kafka") == {"name": "kafka", "host": "172.18.0.2", "port": 22, "username": "learner",
                                    "password": "TheShellPassword", "fingerprint": HOST_FINGERPRINT}
    for sandbox, why in ((sandbox_of(False), "is not running"), (None, "is not running"), (sandbox_of(True, address=""), "has no address")):
        project["now"] = sandbox
        with pytest.raises(core.CoreError, match=why) as refused:
            core.attach("kafka")
        assert "TheShellPassword" not in str(refused.value)
    with pytest.raises(core.CoreError, match="There is no project named other"):
        core.attach("other")


# --- what listens in a sandbox ---------------------------------------------------------------

TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 101 1 0000000000000000 100 0 0 10 0
   1: 0100007F:22B8 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 102 1 0000000000000000 100 0 0 10 0
   2: 020012AC:1388 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 103 1 0000000000000000 100 0 0 10 0
   3: 020012AC:1F41 0A0012AC:C350 01 00000000:00000000 00:00000000 00000000  1000        0 104 1 0000000000000000 100 0 0 10 0
   4: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 105 1 0000000000000000 100 0 0 10 0
   5: 00000000:1B9E 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 106 1 0000000000000000 100 0 0 10 0
   6: not a line at all
"""
TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000000000000:0BB8 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 201 1 0000000000000000 100 0 0 10 0
   1: 00000000000000000000000001000000:2328 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 202 1 0000000000000000 100 0 0 10 0
   2: 0000000000000000FFFF00000100007F:2329 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 203 1 0000000000000000 100 0 0 10 0
   3: 00000000000000000000000000000000:1F40 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 204 1 0000000000000000 100 0 0 10 0
"""


def test_the_socket_tables_are_read_as_the_kernel_writes_them():
    assert [(str(a), port, inode) for a, port, inode in core.listening_sockets(TCP, False)] == [
        ("0.0.0.0", 8000, 101), ("127.0.0.1", 8888, 102), ("172.18.0.2", 5000, 103), ("0.0.0.0", 22, 105), ("0.0.0.0", 7070, 106)]
    assert [(str(a), port) for a, port, _ in core.listening_sockets(TCP6, True)] == [("::", 3000), ("::1", 9000), ("::ffff:127.0.0.1", 9001), ("::", 8000)]


def test_detection_leaves_out_loopback_and_the_sandboxs_own_services():
    assert core.detected_ports(TCP, TCP6) == [{"port": 3000, "inodes": [201], "label": "3000"},
                                              {"port": 5000, "inodes": [103], "label": "5000"},
                                              {"port": 8000, "inodes": [101, 204], "label": "8000"}]


def test_detection_reads_the_sandboxs_tables_from_this_side_and_refuses_a_stopped_one(project, monkeypatch, tmp_path):
    monkeypatch.setattr(core, "PROC", str(tmp_path / "proc"))
    (tmp_path / "proc" / "4242" / "net").mkdir(parents=True)
    (tmp_path / "proc" / "4242" / "net" / "tcp").write_text(TCP)
    assert core.port_detect("kafka") == {"project": "kafka", "running": True, "ports": core.detected_ports(TCP, "")}
    project["now"] = sandbox_of(False)
    with pytest.raises(core.CoreError, match="is not running"):
        core.port_detect("kafka")


# --- ports named by hand ---------------------------------------------------------------------

@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA", str(tmp_path))
    monkeypatch.setattr(core, "LOCKS", str(tmp_path / "locks"))
    monkeypatch.delenv("CL_PASSTHROUGH_PORTS", raising=False)
    for name in ("kafka", "pg"):
        core.write_record(core.new_record(name))
    return tmp_path


def test_a_web_port_is_named_listed_refused_twice_and_removed(registry):
    assert core.port_add("kafka", 8000, " notebook ") == {"project": "kafka", "port": 8000, "label": "notebook",
                                                          "ports": [{"port": 8000, "label": "notebook"}]}
    with pytest.raises(core.CoreError, match="named already"):
        core.port_add("kafka", 8000, "again")
    assert core.port_list("kafka") == {"project": "kafka", "ports": [{"port": 8000, "label": "notebook"}]}
    assert core.port_list("pg") == {"project": "pg", "ports": []}
    assert core.port_remove("kafka", 8000) == {"project": "kafka", "port": 8000, "ports": []}
    assert core.port_remove("kafka", 8000) == {"project": "kafka", "port": 8000, "ports": []}   # gone already


def test_raw_tcp_gets_the_lowest_free_host_port_or_the_one_asked_for(registry):
    assert core.port_add("kafka", 9092, "kafka", passthrough=True)["port"] == 11200
    assert core.port_add("kafka", 9093, "kafka 2", passthrough=True, host_port=11205)["port"] == 11205
    assert core.port_add("pg", 5432, "postgres", passthrough=True)["port"] == 11201
    added = core.port_add("kafka", 9092, "the same port, as a web app")
    web, first, second = ({"port": 9092, "label": "the same port, as a web app"}, {"port": 9092, "label": "kafka", "passthrough": True, "host_port": 11200},
                          {"port": 9093, "label": "kafka 2", "passthrough": True, "host_port": 11205})
    assert added["ports"] == [web, first, second]
    assert core.port_remove("kafka", 11200, passthrough=True)["ports"] == [web, second]
    assert json.loads((registry / "core" / "ports" / "kafka.json").read_text()) == {"ports": [web, second]}


def test_a_host_port_held_by_another_project_is_not_given_out(registry):
    core.port_add("pg", 5432, "postgres", passthrough=True, host_port=11200)
    with pytest.raises(core.CoreError, match="Host port 11200 is taken by project pg"):
        core.port_add("kafka", 9092, "kafka", passthrough=True, host_port=11200)
    assert core.port_add("kafka", 9092, "kafka", passthrough=True)["port"] == 11201
    assert core.port_list("kafka")["ports"] == [{"port": 9092, "label": "kafka", "passthrough": True, "host_port": 11201}]


def test_the_pass_through_range_comes_from_the_setting_and_can_run_out(registry, monkeypatch):
    monkeypatch.setenv("CL_PASSTHROUGH_PORTS", "12000-12001")
    assert [core.port_add("kafka", port, "p", passthrough=True)["port"] for port in (1, 2)] == [12000, 12001]
    with pytest.raises(core.CoreError, match="Every port of the pass-through range 12000-12001 is taken"):
        core.port_add("pg", 3, "p", passthrough=True)
    with pytest.raises(core.CoreError, match="not in the pass-through range 12000-12001"):
        core.port_add("pg", 3, "p", passthrough=True, host_port=11200)


@pytest.mark.parametrize("args, why", [
    (("kafka", 22, "ssh"), "the sandbox's own"), (("kafka", 7070, "files"), "the sandbox's own"), (("kafka", 8443, "editor"), "the sandbox's own"),
    (("kafka", 0, "p"), "port must be"),
    (("kafka", 65536, "p"), "port must be"), (("kafka", True, "p"), "port must be"), (("kafka", "80", "p"), "port must be"),
    (("kafka", 80, ""), "label must be"), (("kafka", 80, "x" * 65), "label must be"), (("kafka", 80, "a\nb"), "label must be"),
    (("kafka", 80, None), "label must be"), (("kafka", 80, "p", "yes"), "passthrough must be"),
    (("kafka", 80, "p", False, 11200), "only for raw TCP"), (("kafka", 80, "p", True, "11200"), "host_port must be"),
    (("other", 80, "p"), "There is no project named other")])
def test_a_port_that_cannot_be_named_is_refused_and_nothing_is_written(registry, args, why):
    with pytest.raises(core.CoreError, match=why):
        core.port_add(*args)
    assert not (registry / "core" / "ports").exists()


def test_the_registry_goes_with_the_project(registry, monkeypatch):
    core.port_add("kafka", 8000, "notebook")
    monkeypatch.setattr(core, "docker", lambda *args, **kwargs: answer(1, "Error response from daemon: No such container: x"))
    core.destroy("kafka")
    assert not (registry / "core" / "ports" / "kafka.json").exists()


# --- the sandbox's init: the Shell password ----------------------------------------------------

def test_the_sandbox_user_gets_the_password_from_its_hash_and_keeps_its_own_without_one(tmp_path):
    ran = []

    def run(command, **given):
        ran.append((command, given["input"]))
        return subprocess.CompletedProcess(command, 0, b"", b"")
    assert sandbox_init.set_password(str(tmp_path), run=run) is False and ran == []       # no file: left as it is
    (tmp_path / "shell_password_hash").write_text("not a hash\n")
    assert sandbox_init.set_password(str(tmp_path), run=run) is False and ran == []
    (tmp_path / "shell_password_hash").write_text(A_HASH + "\n")
    assert sandbox_init.set_password(str(tmp_path), run=run) is True
    assert ran == [(["chpasswd", "-e"], f"learner:{A_HASH}\n".encode())]
