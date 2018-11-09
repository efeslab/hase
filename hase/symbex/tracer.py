from __future__ import absolute_import, division, print_function

import ctypes
import gc
import logging
import os
import signal
from bisect import bisect_right
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import angr
import archinfo
import claripy
from angr import SimState
from angr import sim_options as so
from angr.state_plugins.sim_action import SimActionExit
from capstone import x86_const
from pygdbmi.gdbcontroller import GdbController

from ..errors import HaseError
from ..pt.events import Instruction, InstructionClass
from ..pwn_wrapper import ELF, Coredump, Mapping
from .filter import FilterTrace
from .hook import addr_symbols, all_hookable_symbols
from .state import State, StateManager

l = logging.getLogger("hase")

ELF_MAGIC = b"\x7fELF"


class HaseTimeoutException(Exception):
    pass


def timeout(seconds=10):
    def wrapper(func):
        original_handler = signal.getsignal(signal.SIGALRM)

        def timeout_handler(signum, frame):
            signal.signal(signal.SIGALRM, original_handler)
            raise HaseTimeoutException("Timeout")

        def inner(*args, **kwargs):
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(seconds)
            try:
                res = func(*args, **kwargs)
            finally:
                signal.alarm(0)
            return res

        return inner

    return wrapper


class CoredumpGDB(object):
    def __init__(self, elf, coredump):
        # type: (ELF, Coredump) -> None
        self.coredump = coredump
        self.elf = elf
        self.corefile = self.coredump.file.name
        self.execfile = self.elf.file.name
        # XXX: use --nx will let manually set debug-file-directory
        # and unknown cause for not showing libc_start_main and argv
        # FIXME: get all response and retry if failed
        self.gdb = GdbController(gdb_args=["--quiet", "--interpreter=mi2"])
        # pwnlibs response
        self.get_response()
        self.setup_gdb()

    def setup_gdb(self):
        # type: () -> None
        self.write_request("file {}".format(self.execfile))
        self.write_request("core {}".format(self.corefile))

    def get_response(self):
        # type: () -> List[Dict[str, Any]]
        resp: List[Dict[str, Any]] = []
        while True:
            try:
                resp += self.gdb.get_gdb_response()
            except Exception:
                break
        return resp

    def write_request(self, req, **kwargs):
        # type: (str, **Any) -> List[Dict[str, Any]]
        self.gdb.write(req, timeout_sec=1, read_response=False, **kwargs)
        resp = self.get_response()
        return resp

    def parse_frame(self, r):
        # type: (str) -> Dict[str, Any]
        attrs: Dict[str, Any] = {}
        # NOTE: #n  addr in func (args=args[ <name>][@entry=v]) at source_code[:line]\n
        r = r.replace("\\n", "")
        attrs["index"] = r.partition(" ")[0][1:]
        r = r.partition(" ")[2][1:]
        attrs["addr"] = r.partition(" ")[0]
        r = r.partition(" ")[2]
        r = r.partition(" ")[2]
        attrs["func"] = r.partition(" ")[0]
        r = r.partition(" ")[2]
        args = r.partition(")")[0][1:].split(", ")
        args_list = []

        # NOTE: remove <xxx>
        def remove_comment(arg):
            # type: (str) -> str
            if arg.find("<") != -1:
                arg = arg.partition("<")[0]
                arg = arg.replace(" ", "")
            return arg

        for arg in args:
            if arg.find("@") != -1:
                name, _, entry_ = arg.partition("@")
            else:
                name = arg
                entry_ = ""
            name, _, value = name.partition("=")
            value = remove_comment(value)
            if entry_:
                _, _, entry = entry_.partition("=")
                entry = remove_comment(entry)
                args_list.append([name, value, entry])
            else:
                args_list.append([name, value, ""])
        attrs["args"] = args_list  # type: ignore
        r = r.partition(")")[2]
        r = r.partition(" ")[2]
        r = r.partition(" ")[2]
        if r.find(":") != -1:
            source, _, line = r.partition(":")
        else:
            source = r
            line = "?"
        attrs["file"] = source
        attrs["line"] = line
        return attrs

    def parse_addr(self, r):
        # type: (str) -> int
        # $n = (...) 0xaddr <name>
        l = r.split(" ")
        for blk in l:
            if blk.startswith("0x"):
                return int(blk, 16)
        return 0

    def parse_offset(self, r):
        # type: (str) -> int
        # addr <+offset>:  inst
        l = r.split(" ")
        for blk in l:
            if blk.startswith("<+"):
                idx = blk.find(">")
                return int(blk[2:idx])
        return 0

    def backtrace(self):
        # type: () -> List[Dict[str, Any]]
        resp = self.write_request("where")
        bt = []
        for r in resp:
            payload = r["payload"]
            if payload and payload[0] == "#":
                print(payload)
                bt.append(self.parse_frame(payload))
        return bt

    def get_symbol(self, addr):
        # type: (int) -> str
        resp = self.write_request("info symbol {}".format(addr))
        return resp[1]["payload"]

    def get_reg(self, reg_name):
        # type: (str) -> int
        resp = self.write_request("info reg {}".format(reg_name))
        if len(resp) < 5 or not resp[2]["payload"].startswith("\\t"):
            return 0
        return int(resp[2]["payload"][2:].split(" ")[0], 16)

    def get_stack_base(self, n):
        # type: (int) -> Tuple[int, int]
        self.write_request("select-frame {}".format(n))
        rsp_value = self.get_reg("rsp")
        rbp_value = self.get_reg("rbp")
        return rsp_value, rbp_value

    def get_func_range(self, name):
        # type: (str) -> List[int]
        # FIXME: Not a good idea. Maybe some gdb extension?
        r1 = self.write_request("print &{}".format(name))
        addr = self.parse_addr(r1[1]["payload"])
        r2 = self.write_request("disass {}".format(name))
        size = self.parse_offset(r2[-3]["payload"])
        return [addr, size + 1]


class CoredumpAnalyzer(object):
    def __init__(self, elf, coredump):
        # type: (ELF, Coredump) -> None
        self.coredump = coredump
        self.elf = elf
        self.gdb = CoredumpGDB(elf, coredump)
        self.backtrace = self.gdb.backtrace()
        self.argc = self.coredump.argc
        self.argv = [self.read_argv(i) for i in range(self.argc)]
        self.argv_addr = [self.read_argv_addr(i) for i in range(self.argc)]

    def read_stack(self, addr, length=0x1):
        # type: (int, int) -> str
        # NOTE: a op b op c will invoke weird typing
        assert self.coredump.stack.start <= addr < self.coredump.stack.stop
        offset = addr - self.coredump.stack.start
        return self.coredump.stack.data[offset : offset + length]

    def read_argv(self, n):
        # type: (int) -> str
        assert 0 <= n < self.coredump.argc
        return self.coredump.string(self.coredump.argv[n])

    def read_argv_addr(self, n):
        # type: (int) -> str
        assert 0 <= n < self.coredump.argc
        return self.coredump.argv[n]

    @property
    def env(self):
        return self.coredump.env

    @property
    def registers(self):
        return self.coredump.registers

    @property
    def stack_start(self):
        return self.coredump.stack.start

    @property
    def stack_stop(self):
        return self.coredump.stack.stop

    def call_argv(self, name):
        # type: (str) -> Optional[List[Optional[int]]]
        for bt in self.backtrace:
            if bt["func"] == name:
                args: List[Optional[int]] = []
                for _, value, entry in bt["args"]:
                    if entry:
                        args.append(int(entry, 16))
                    else:
                        if value != "":
                            args.append(int(value, 16))
                        else:
                            args.append(None)
                return args
        return None

    def stack_base(self, name):
        # type: (str) -> Tuple[Optional[int], Optional[int]]
        for bt in self.backtrace:
            if bt["func"] == name:
                return self.gdb.get_stack_base(int(bt["index"]))
        return (None, None)


def build_load_options(mappings):
    # type: (List[Mapping]) -> dict
    """
    Extract shared object memory mapping from coredump
    """
    # FIXME: actually this library path different will cause
    # simulation path different? need re-record if original
    # executable is recompiled
    main = mappings[0]
    lib_opts: Dict[str, Dict[str, int]] = {}
    force_load_libs = []
    for m in mappings[1:]:
        if not m.path.startswith("/") or m.path in lib_opts:
            continue
        with open(m.path, "rb") as f:
            magic = f.read(len(ELF_MAGIC))
            if magic != ELF_MAGIC:
                continue
        lib_opts[m.path] = dict(custom_base_addr=m.start)
        force_load_libs.append(m.path)

    # TODO: extract libraries from core dump instead ?
    return dict(
        main_opts={"custom_base_addr": main.start},
        force_load_libs=force_load_libs,
        lib_opts=lib_opts,
        load_options={"except_missing_libs": True},
    )


class Tracer(object):
    def __init__(self, executable, trace, coredump):
        # type: (str, List[Instruction], Coredump) -> None
        self.executable = executable
        options = build_load_options(coredump.mappings)
        self.project = angr.Project(executable, **options)

        self.coredump = coredump
        self.debug_unsat: Optional[SimState] = None

        self.trace = trace

        assert self.project.loader.main_object.os.startswith("UNIX")

        self.elf = ELF(executable)

        start = self.elf.symbols.get("_start")
        main = self.elf.symbols.get("main")

        self.cdanalyzer = CoredumpAnalyzer(self.elf, self.coredump)

        for (idx, event) in enumerate(self.trace):
            if event.ip == start or event.ip == main:
                self.trace = trace[idx:]

        add_options = {
            so.TRACK_JMP_ACTIONS,
            so.CONSERVATIVE_READ_STRATEGY,
            so.CONSERVATIVE_WRITE_STRATEGY,
            so.BYPASS_UNSUPPORTED_IRCCALL,
            so.BYPASS_UNSUPPORTED_IRDIRTY,
            so.CONSTRAINT_TRACKING_IN_SOLVER,
            # so.DOWNSIZE_Z3,
        }

        remove_simplications = {
            so.LAZY_SOLVES,
            so.EFFICIENT_STATE_MERGING,
            so.TRACK_CONSTRAINT_ACTIONS,
            # so.ALL_FILES_EXIST, # the problem is, when having this, simfd either None or exist, no If
        } | so.simplification

        self.cfg = self.project.analyses.CFGFast(show_progressbar=True)

        self.use_hook = True
        self.omitted_section: List[List[int]] = []

        if self.use_hook:
            self.hooked_symbols = all_hookable_symbols.copy()
            self.setup_hook()
        else:
            self.hooked_symbols = {}
            self.project._sim_procedures = {}

        self.from_initial = True

        self.filter = FilterTrace(
            self.project,
            self.cfg,
            self.trace,
            self.hooked_symbols,
            self.cdanalyzer.gdb,
            self.omitted_section,
            self.from_initial,
            self.elf.statically_linked,
            self.cdanalyzer.backtrace,
        )

        self.old_trace = self.trace
        self.trace, self.trace_idx, self.hook_target = self.filter.filtered_trace()
        self.hook_plt_idx = list(self.hook_target.keys())
        self.hook_plt_idx.sort()

        start_address = self.trace[0].ip

        if self.filter.start_funcname == "main":
            args = self.cdanalyzer.call_argv("main")
            if not args:
                self.start_state = self.project.factory.blank_state(
                    addr=start_address,
                    add_options=add_options,
                    remove_options=remove_simplications,
                )
            else:
                # NOTE: gdb sometimes take this wrong
                args[0] = self.coredump.argc
                self.start_state = self.project.factory.call_state(
                    start_address,
                    *args,
                    add_options=add_options,
                    remove_options=remove_simplications
                )
            rsp, rbp = self.cdanalyzer.stack_base("main")
            # TODO: or just stop?
            if not rbp:
                rbp = 0x7FFFFFFCF00
            if not rsp:
                rsp = 0x7FFFFFFCF00
        else:
            rsp, rbp = self.cdanalyzer.stack_base(self.filter.start_funcname)
            if not rbp:
                rbp = 0x7FFFFFFCF00
            if not rsp:
                rsp = 0x7FFFFFFCF00
            self.start_state = self.project.factory.blank_state(
                addr=start_address,
                add_options=add_options,
                remove_options=remove_simplications,
            )

        l.warning("{} {}".format(len(self.trace), len(self.old_trace)))

        if self.filter.is_start_entry:
            self.start_state.regs.rsp = rbp + 8
        else:
            self.start_state.regs.rsp = rsp
            self.start_state.regs.rbp = rbp

        self.setup_argv()
        self.simgr = self.project.factory.simgr(
            self.start_state, save_unsat=True, hierarchy=False, save_unconstrained=True
        )

        # self.setup_breakpoint()

        # For debugging
        # self.project.pt = self
        self.constraints_index: Dict[int, int] = {}

    def setup_argv(self):
        # type: () -> None
        # argv follows argc
        argv_addr = self.coredump.argc_address + ctypes.sizeof(ctypes.c_int)
        # TODO: if argv is modified by users, this won't help
        for i in range(len(self.coredump.argv)):
            self.start_state.memory.store(
                argv_addr + i * 8, self.coredump.argv[i], endness=archinfo.Endness.LE
            )
            self.start_state.memory.store(
                self.coredump.argv[i],
                self.coredump.string(self.coredump.argv[i])[::-1],
                endness=archinfo.Endness.LE,
            )

    def setup_hook(self):
        # type: () -> None
        self.hooked_symbols.pop("abort", None)
        self.hooked_symbols.pop("__assert_fail", None)
        self.hooked_symbols.pop("__stack_chk_fail", None)
        try:
            abort_addr = self.project.loader.find_symbol("abort").rebased_addr
            assert_addr = self.project.loader.find_symbol("__assert_fail").rebased_addr
            stack_addr = self.project.loader.find_symbol(
                "__stack_chk_fail"
            ).rebased_addr
            self.project._sim_procedures.pop(abort_addr, None)
            self.project._sim_procedures.pop(assert_addr, None)
            self.project._sim_procedures.pop(stack_addr, None)
        except:
            pass

        for symname, func in self.hooked_symbols.items():
            self.project.hook_symbol(symname, func())
        for symname in addr_symbols:
            if symname in self.hooked_symbols.keys():
                r = self.cdanalyzer.gdb.get_func_range(symname)
                func = self.hooked_symbols[symname]
                if r != [0, 0]:
                    self.project.hook(r[0], func(), length=r[1])
                    self.omitted_section.append(r)

    def setup_breakpoint(self):
        self.start_state.inspect.b(
            "mem_write", when=angr.BP_AFTER, action=Tracer.inspect_mem_write
        )

    @staticmethod
    def inspect_mem_write(state):
        length = state.inspect.mem_write_length
        if not state.solver.symbolic(length) and state.solver.eval(length) > 0x1000:
            l.warning(
                hex(state.addr)
                + ": write "
                + str(state.inspect.mem_write_expr)
                + "with length "
                + str(state.inspect.mem_write_length)
            )
            raw_input()

    def test_rep_ins(self, state):
        # type: (SimState) -> bool
        # NOTE: rep -> sat or unsat
        capstone = state.block().capstone
        first_ins = capstone.insns[0].insn
        # NOTE: maybe better way is use prefix == 0xf2, 0xf3 (crc32 exception)
        ins_repr = first_ins.mnemonic
        return ins_repr.startswith("rep")

    def repair_hook_return(self, state, index):
        # given a force_jump, from hook -> last
        # caller -> plt -> hook
        if self.project.is_hooked(state.addr):
            """
                it might be that call -> jmp -> real plt
                or it could be add rsp 0x8, jmp -> directly ret
                the only way seems to be sacrifice some execution 
                or look back to old_trace
            """
            old_branch_idx = self.trace_idx[index] - 1
            # should be plt
            plt_idx = bisect_right(self.hook_plt_idx, old_branch_idx) - 1
            ret_addr = self.hook_target[self.hook_plt_idx[plt_idx]]
            # like a ret
            new_state = state.copy()
            new_state.regs.rsp += 8
            new_state.regs.ip = ret_addr
            return True, new_state
        return False, None

    def repair_exit_handler(self, state, step):
        # type: (SimState, SimState) -> SimState
        artifacts = getattr(step, "artifacts", None)
        if (
            artifacts
            and "procedure" in artifacts.keys()
            and artifacts["name"] == "exit"
        ):
            if len(state.libc.exit_handler):
                addr = state.libc.exit_handler[0]
                step = self.project.factory.successors(
                    state, num_inst=1, force_addr=addr
                )
        return step

    def repair_alloca_ins(self, state):
        # type: (SimState) -> None
        # NOTE: alloca problem, focus on sub rsp, rax
        # Typical usage: alloca(strlen(x))
        capstone = state.block().capstone
        first_ins = capstone.insns[0].insn
        if first_ins.mnemonic == "sub":
            if (
                first_ins.operands[0].reg
                in (x86_const.X86_REG_RSP, x86_const.X86_REG_RBP)
                and first_ins.operands[1].type == 1
            ):
                reg_name = first_ins.reg_name(first_ins.operands[1].reg)
                reg_v = getattr(state.regs, reg_name)
                if state.solver.symbolic(reg_v):
                    setattr(state.regs, reg_name, state.libc.max_str_len)

    def repair_jump_ins(self, state, previous_instruction, instruction):
        # type: (SimState, Instruction, Instruction) -> Tuple[bool, str]
        # ret: force_jump
        # NOTE: typical case: switch(getchar())

        if (
            previous_instruction.iclass == InstructionClass.ptic_other
            or previous_instruction.ip != state.addr
        ):
            return False, ""
        jump_ins = ["jmp", "call"]  # currently not deal with jcc regs
        capstone = state.block().capstone
        first_ins = capstone.insns[0].insn
        ins_repr = first_ins.mnemonic

        if ins_repr.startswith("ret"):
            if not state.solver.symbolic(state.regs.rsp):
                mem = state.memory.load(state.regs.rsp, 8)
                jump_target = 0
                if not state.solver.symbolic(mem):
                    jump_target = state.solver.eval(mem)
                if jump_target != instruction.ip:
                    return True, "ret"
                else:
                    return True, "ok"
            else:
                return True, "ret"

        for ins in jump_ins:
            if ins_repr.startswith(ins):
                # call rax
                if first_ins.operands[0].type == 1:
                    reg_name = first_ins.op_str
                    reg_v = getattr(state.regs, reg_name)
                    if (
                        state.solver.symbolic(reg_v)
                        or state.solver.eval(reg_v) != instruction.ip
                    ):
                        setattr(state.regs, reg_name, instruction.ip)
                        return True, ins

                # jmp 0xaabb
                if first_ins.operands[0].type == 2:
                    return True, ins

                # jmp [base + index*scale + disp]
                if first_ins.operands[0].type == 3:
                    self.last_jump_table = state
                    mem = first_ins.operands[0].value.mem
                    target = mem.disp
                    if mem.index:
                        reg_index_name = first_ins.reg_name(mem.index)
                        reg_index = getattr(state.regs, reg_index_name)
                        if state.solver.symbolic(reg_index):
                            return True, ins
                        else:
                            target += state.solver.eval(reg_index) * mem.scale
                    if mem.base:
                        reg_base_name = first_ins.reg_name(mem.base)
                        reg_base = getattr(state.regs, reg_base_name)
                        if state.solver.symbolic(reg_base):
                            return True, ins
                        else:
                            target += state.solver.eval(reg_base)
                    ip_mem = state.memory.load(target, 8, endness="Iend_LE")
                    if not state.solver.symbolic(ip_mem):
                        jump_target = state.solver.eval(ip_mem)
                        if jump_target != instruction.ip:
                            return True, ins
                        else:
                            return True, "ok"
                    else:
                        return True, ins
        return False, "ok"

    def repair_ip(self, state):
        # type: (SimState) -> int
        try:
            addr = state.solver.eval(state._ip)
            # NOTE: repair IFuncResolver
            if (
                self.project.loader.find_object_containing(addr)
                == self.project.loader.extern_object
            ):
                func = self.project._sim_procedures.get(addr, None)
                if func:
                    funcname = func.kwargs["funcname"]
                    libf = self.project.loader.find_symbol(funcname)
                    if libf:
                        addr = libf.rebased_addr
        except Exception:
            # NOTE: currently just try to repair ip for syscall
            addr = self.debug_state[-2].addr
        return addr

    def repair_func_resolver(self, state, step):
        # type: (SimState, SimState) -> SimState
        artifacts = getattr(step, "artifacts", None)
        if (
            artifacts
            and "procedure" in artifacts.keys()
            and artifacts["name"] == "IFuncResolver"
        ):
            func = self.filter.find_function(self.debug_state[-2].addr)
            if func:
                addr = self.project.loader.find_symbol(func.name).rebased_addr
                step = self.project.factory.successors(
                    state, num_inst=1, force_addr=addr
                )
            else:
                raise HaseError("Cannot resolve function")
        return step

    def last_match(self, choice, instruction):
        # type: (SimState, Instruction) -> bool
        # if last trace is A -> A
        if (
            instruction == self.trace[-1]
            and len(self.trace) > 2
            and self.trace[-1].ip == self.trace[-2].ip
        ):
            if choice.addr == instruction.ip:
                l.debug("jump 0%x -> 0%x", choice.addr, choice.addr)
                return True
        return False

    def jump_match(self, old_state, choice, previous_instruction, instruction):
        # type: (SimState, SimState, Instruction, Instruction) -> bool
        if choice.addr == instruction.ip:
            l.debug("jump 0%x -> 0%x", previous_instruction.ip, choice.addr)
            return True
        return False

    def jump_was_not_taken(self, old_state, new_state):
        # type: (SimState, SimState) -> bool
        # was the last control flow change an exit vs call/jump?
        ev = new_state.history.recent_events[-1]
        if not isinstance(ev, SimActionExit):  # and len(instructions) == 1
            return False
        instructions = old_state.block().capstone.insns
        size = instructions[0].insn.size
        return (new_state.addr - size) == old_state.addr

    def repair_satness(self, old_state, new_state):
        if not new_state.solver.satisfiable():  # type: ignore
            sat_constraints = old_state.solver._solver.constraints
            """
            unsat_constraints = list(new_state.solver._solver.constraints)
            sat_uuid = map(lambda c: c.uuid, sat_constraints)
            for i, c in enumerate(unsat_constraints):
                if c.uuid not in sat_uuid:
                    unsat_constraints[i] = claripy.Not(c)
            """
            new_state.solver._stored_solver = old_state.solver._solver.branch()

            if not self.debug_unsat:  # type: ignore
                self.debug_sat = old_state
                self.debug_unsat = new_state

    def record_constraints_index(self, old_state: SimState, new_state: SimState, index: int) -> None:
        sat_uuid = map(lambda c: c.uuid, old_state.solver.constraints)
        unsat_constraints = list(new_state.solver.constraints)
        for c in unsat_constraints:
            if c.uuid not in sat_uuid:
                self.constraints_index[c] = index

    def repair_ip_at_syscall(self, old_state: SimState, new_state: SimState) -> None:
        capstone = old_state.block().capstone
        first_ins = capstone.insns[0].insn
        ins_repr = first_ins.mnemonic
        if ins_repr.startswith("syscall"):
            new_state.regs.ip_at_syscall = new_state.ip

    def post_execute(self, old_state, state):
        self.repair_satness(old_state, state)
        self.repair_ip_at_syscall(old_state, state)


    def execute(
        self,
        state: SimState,
        previous_instruction: Instruction,
        instruction: Instruction,
        index: int,
    ) -> Tuple[SimState, SimState]:
        self.debug_state.append(state)
        force_jump, force_type = self.repair_jump_ins(
            state, previous_instruction, instruction
        )
        self.repair_alloca_ins(state)
        addr = previous_instruction.ip

        try:
            step = self.project.factory.successors(
                state, num_inst=1, force_addr=addr
            )
        except Exception:
            new_state = state.copy()
            new_state.regs.ip = instruction.ip
            self.post_execute(state, new_state)
            return state, new_state
        if force_jump:
            new_state = state.copy()
            if force_type == "call":
                new_state.regs.rsp -= 8
                ret_addr = state.addr + state.block().capstone.insns[0].size
                new_state.memory.store(
                    new_state.regs.rsp, ret_addr, endness="Iend_LE"
                )
            elif force_type == "ret":
                new_state.regs.rsp += 8
            new_state.regs.ip = instruction.ip
            all_choices = {"sat": [new_state], "unsat": [], "unconstrained": []}
            choices = [new_state]
        else:
            step = self.repair_func_resolver(state, step)
            step = self.repair_exit_handler(state, step)

            all_choices = {
                "sat": step.successors,
                "unsat": step.unsat_successors,
                "unconstrained": step.unconstrained_successors,
            }
            choices = []
            choices += all_choices["sat"]
            choices += all_choices["unsat"]

        old_state = state
        l.warning(
            repr(state)
            + " "
            +
            # repr(all_choices) + ' ' +
            repr(instruction)
            + "\n"
        )
        for choice in choices:
            if self.last_match(choice, instruction):
                return choice, choice
            if self.jump_match(
                old_state, choice, previous_instruction, instruction
            ):
                self.post_execute(old_state, choice)
                return old_state, choice
        new_state = state.copy()
        new_state.regs.ip = instruction.ip
        return state, new_state

    def valid_address(self, address):
        # type: (int) -> bool
        return self.project.loader.find_object_containing(address)

    def constrain_registers(self, state):
        # type: (State) -> None
        # FIXME: if exception caught is omitted by hook?
        # If same address, then give registers
        if state.registers["rip"].value == self.coredump.registers["rip"]:
            # don't give rbp, rsp
            assert state.registers["rsp"].value == self.coredump.registers["rsp"]
            registers = [
                "gs",
                "rip",
                "rdx",
                "r15",
                "rax",
                "rsi",
                "rcx",
                "r14",
                "fs",
                "r12",
                "r13",
                "r10",
                "r11",
                "rbx",
                "r8",
                "r9",
                "eflags",
                "rdi",
            ]
            for name in registers:
                state.registers[name] = self.coredump.registers[name]
        else:
            l.warning("RIP mismatch.")

    def run(self):
        # type: () -> StateManager
        simstate = self.simgr.active[0]
        states = StateManager(self, len(self.trace))
        states.add_major(State(0, None, self.trace[0], None, simstate))
        self.debug_unsat: Optional[SimState] = None
        self.debug_state: deque = deque(maxlen=5)
        self.skip_addr: Dict[int, int] = {}
        cnt = 0
        interval = max(1, len(self.trace) // 200)
        length = len(self.trace) - 1

        for previous_idx, instruction in enumerate(self.trace[1:]):
            previous_instruction = self.trace[previous_idx]

            cnt += 1
            if not cnt % 500:
                l.warning("Do a garbage collection")
                gc.collect()
            l.debug(
                "look for jump: 0x%x -> 0x%x"
                % (previous_instruction.ip, instruction.ip)
            )
            assert self.valid_address(previous_instruction.ip) and self.valid_address(
                instruction.ip
            )
            old_simstate, new_simstate = self.execute(simstate, previous_instruction, instruction, cnt)
            simstate = new_simstate
            if cnt % interval == 0 or length - cnt < 15:
                states.add_major(
                    State(
                        cnt,
                        previous_instruction,
                        instruction,
                        old_simstate,
                        new_simstate,
                    )
                )
        self.constrain_registers(states.major_states[-1])

        return states
