import logging
logging.basicConfig(level=logging.INFO)
import magma as m
import mantle
import fault
import os

# Base instructions:
#  1. Phi
#  2. Branch on condition
#  3. Arithmetic?
#  4. Assign
class PipelinedAdder32(m.Circuit):
    io = m.IO(
            I0=m.In(m.Bits[32]),
            I1=m.In(m.Bits[32]),
            O=m.Out(m.Bits[32]),
            RST=m.In(m.Reset),
            CLK=m.In(m.Clock))

    mres = m.uint(io.I0) * m.uint(io.I1)
    res = mantle.Register(32)
    m.wire(mres, res.I)
    m.wire(res.O, io.O)


class HLSArg:

    def __init__(self, name, width, is_input):
        self.is_input = is_input
        self.name = name
        self.width = width

    def uses(self, v):
        return False

    def is_in(self):
        return self.is_input

    def is_out(self):
        return not self.is_in()

    def __repr__(self):
        return self.name + " : " + str(self.width)

class HLSMicroArchitecture:

    def __init__(self, prog):
        self.prog = prog
        self.value_mapping = {}
        self.num_stages = 0
        self.II = 1
        self.functional_unit_mapping = {}
        self.producer_units = {}
        self.stage_active_registers = {}

    def get_producer_unit(self, instr):
        return self.producer_units[instr]

    def set_producer_unit(self, instr, unit):
        self.producer_units[instr] = unit

    def set_producer(self, instr, wire_out):
        self.functional_unit_mapping[instr] = wire_out
        if not instr in self.value_mapping:
            self.value_mapping[instr] = {}
        self.value_mapping[instr][self.sched.production_time(instr)] = wire_out

    def stage_active_wire(self, stage_num):
        assert(stage_num in self.stage_active_registers)
        return self.stage_active_registers[stage_num]

    def wire_at(self, time, value):
        assert(value in self.value_mapping)
        if not time in self.value_mapping[value]:
            print('Error:', time, 'is not in', self.value_mapping[value])
        assert(time in self.value_mapping[value])
        return self.value_mapping[value][time]

def is_function_arg(v):
    return isinstance(v, HLSArg)

def latency(v):
    if is_function_arg(v):
        return 1
    else:
        assert(isinstance(v, HLSInstrInstance))
        return v.latency

class HLSSchedule:

    def __init__(self, st, et):
        self.start_times = st
        self.end_times = et

    def last_use_time(self, val):
        last_time = -1
        for user in self.start_times:
            if user.uses(val):
                if self.start_times[user] > last_time:
                    last_time = self.start_times[user]
        print('Error: No users of:', val)
        assert(last_time >= 0)
        return last_time

    def contains(self, v):
        return v in self.start_times and v in self.end_times

    def init_time(self, v):
        return self.start_times[v]

    def production_time(self, v):
        for s in self.end_times:
            if s == v:
                return self.end_times[s]
        print('Error: No production time for: ', v)
        assert(False)

    def num_stages(self):
        smax = 0

        for e in self.end_times:
            if self.end_times[e] > smax:
                smax = self.end_times[e]

        return smax + 1

def asap_schedule(prog):
    start_times = {}
    end_times = {}
    active = set()
    done = set()
    for a in prog.args:
        if a.is_in():
            active.add(a)
        else:
            assert(a.is_out())
            done.add(a)

    unscheduled = set(prog.instrs)
    
    sched_list = []

    for a in prog.args:
        if a.is_in():
            start_times[a] = 0
            active.add(a)

    time = 1
    while len(unscheduled) > 0:
        for v in active:
            completion_time = start_times[v] + latency(v)
            assert(completion_time >= start_times[v])
            print(v, 'is active at time: ', time, 'will complete at time:', completion_time)
            if completion_time == time:
                assert(not (v in unscheduled))
                end_times[v] = time
                done.add(v)

        found = True
        while found:
            found = False
            for instr in unscheduled:
                all_args_scheduled = True
                for arg in instr.args:
                    if not (arg in done):
                        print('Arg: ', arg, 'of', instr, 'is not scheduled at time: ', time)
                        print('Done...')
                        for d in done:
                            print('\t', d)
                        all_args_scheduled = False
                        break
                if all_args_scheduled:
                    found = True
                    print('Scheduling', instr)
                    unscheduled.remove(instr)
                    start_times[instr] = time
                    if instr.latency == 0:
                        end_times[instr] = start_times[instr]
                        done.add(instr)
                    else:
                        active.add(instr)
                    break

        time += 1
        # assert(time < 3)

    for v in active:
        end_times[v] = start_times[v] + latency(v)

    print('Schedule...')
    for s in start_times:
        print(s, '->', start_times[s])
    print('End times...')
    for s in end_times:
        print(s, '->', end_times[s])

    return HLSSchedule(start_times, end_times)

def generate_microarchitecture(prog):
    arch = HLSMicroArchitecture(prog)

    sched = asap_schedule(prog)
    arch.sched = sched;

    class Main(m.Circuit):
        name = prog.name

        io = m.IO(start=m.In(m.Bit),
                done=m.Out(m.Bit),
                RST=m.In(m.Reset),
                CLK=m.In(m.Clock))

        for arg in prog.args:
            if arg.is_in():
                io += m.IO(**{arg.name : m.In(m.Bits[arg.width])})
            else:
                assert(arg.is_out())
                io += m.IO(**{arg.name : m.Out(m.Bits[arg.width])})

        # Create stage active wires
        for s in range(0, sched.num_stages()):
            if s == 0:
                arch.stage_active_registers[s] = io.start
            else:
                sreg = mantle.Register(None)
                m.wire(sreg.I, arch.stage_active_registers[s - 1])
                arch.stage_active_registers[s] = sreg.O

        # Populate value mapping
        for arg in prog.args:
            if arg.is_in():
                sreg = mantle.DefineRegister(arg.width)()
                arch.functional_unit_mapping[arg] = sreg.O
                m.wire(sreg.I, getattr(io, arg.name))
                arch.value_mapping[arg] = {}
                arch.value_mapping[arg][sched.production_time(arg)] = arch.functional_unit_mapping[arg]

        for i in prog.instrs:
            if i.has_output():
                arch.value_mapping[i] = {}

        for instr in prog.instrs:
            if instr.op == "uadd":
                res = mantle.Add(32)
                # instr.args[0].width)
                arch.set_producer_unit(instr, res)
                arch.set_producer(instr, m.bits(res.O))
            elif instr.op == "umul_l1":
                res = PipelinedAdder32()
                arch.set_producer_unit(instr, res)
                arch.set_producer(instr, m.bits(res.O))

        for arg in prog.args:
            if arg.is_in():
                ptime = sched.production_time(arg)
                wire_before = arch.functional_unit_mapping[arg]

                for stage in range(ptime + 1, sched.last_use_time(arg) + 1):
                    print('setting value of', arg, 'at stage', stage)
                    # assert(False)
                    wire_next = mantle.Register(arg.width)
                    arch.value_mapping[arg][stage] = wire_next.O
                    m.wire(wire_next.I, wire_before)
                    wire_before = wire_next

        for instr in prog.instrs:
            if instr.has_output():
                ptime = sched.production_time(instr)
                wire_before = arch.wire_at(ptime, instr)

                for stage in range(ptime + 1, sched.last_use_time(instr) + 1):
                    print('setting value of', instr, 'at stage', stage)
                    # assert(False)
                    wire_next = mantle.Register(instr.args[0].width)
                    arch.value_mapping[instr][stage] = wire_next.O
                    m.wire(wire_next.I, wire_before)
                    wire_before = wire_next

        for instr in prog.instrs:
            if instr.op == "write":
                print('Wiring up: ', instr)
                m.wire(getattr(io, instr.args[0].name), arch.wire_at(sched.init_time(instr), instr.args[1]))
            elif instr.op == "uadd":
                st = sched.init_time(instr)
                arg_a = arch.wire_at(st, instr.args[0])
                arg_b = arch.wire_at(st, instr.args[1])
                res = arch.get_producer_unit(instr)

                assert(res != None)

                m.wire(arg_a, res.I0)
                m.wire(arg_b, res.I1)


            elif instr.op == "umul_l1":
                st = sched.init_time(instr)
                arg_a = arch.wire_at(st, instr.args[0])
                arg_b = arch.wire_at(st, instr.args[1])
                mres = arch.get_producer_unit(instr)
                m.wire(arg_a, mres.I0)
                m.wire(arg_b, mres.I1)

            else:
                print('Error: Unsupported instr: ', instr)
                assert(False)

        io.done @= arch.stage_active_wire(sched.num_stages() - 1)

    arch.main = Main
    return arch

class HLSInstrInstance:

    def __init__(self, name, op, args):
        self.op = op
        self.name = name
        self.args = args
        self.predicate = ""
        self.latency = 0

    def uses(self, v):
        for a in self.args:
            if a == v:
                return True
        return False

    def has_output(self):
        return self.op != "write"

    def __repr__(self):
        sargs = []
        for a in self.args:
            sargs.append(a.__repr__())
        return self.name + " " + self.op + " " + ", ".join(sargs)

class HLSProg:

    def __init__(self, name):
        self.name = name
        self.instrs = []
        self.args = []
        self.trip_count = 1
        self.num = 0

    def add_instr(self, name, args):
        n = self.num
        self.num += 1
        instr = HLSInstrInstance("instr_" + str(n), name, args)
        self.instrs.append(instr)
        return instr

    def add_out(self, name, width):
        arg = HLSArg(name, width, False)
        self.args.append(arg)
        return arg;

    def add_in(self, name, width):
        arg = HLSArg(name, width, True)
        self.args.append(arg)
        return arg

    def compile(self):
        arch = generate_microarchitecture(self)
        main = arch.main


        return main 

def POSEDGE(t):
    t.step(2)

def test_hls():
    prog = HLSProg("inout_test")
    i = prog.add_in("input_val", 1)
    o = prog.add_out("out", 1)

    wr = prog.add_instr("write", [o, i])

    result = prog.compile();

    print('Result: ', result)

    tester = fault.Tester(result, result.CLK)

    tester.circuit.CLK = 0
    tester.circuit.RESET = 1
    tester.circuit.start = 0

    POSEDGE(tester)

    # tester.circuit.out_valid.expect(0)

    tester.circuit.input_val = 23
    tester.circuit.start = 1

    POSEDGE(tester)

    tester.circuit.out.expect(23)
    tester.circuit.done.expect(1)

    tester.compile_and_run("verilator", magma_opts={"inline":True}, flags=["-Wno-fatal"])


def test_hls_add():
    os.system("rm -f ./build")

    prog = HLSProg("ab_add")
    a = prog.add_in("a", 32)
    b = prog.add_in("b", 32)
    o = prog.add_out("c", 32)

    res = prog.add_instr("uadd", [a, b])
    wr = prog.add_instr("write", [o, res])

    result = prog.compile();

    print('Result: ', result)

    tester = fault.Tester(result, result.CLK)

    tester.circuit.CLK = 0
    tester.circuit.RESET = 1
    tester.circuit.start = 0

    POSEDGE(tester)
    POSEDGE(tester)
    POSEDGE(tester)

    tester.circuit.done.expect(0)
    tester.circuit.a = 10
    tester.circuit.b = 15
    tester.circuit.start = 1

    POSEDGE(tester)

    tester.circuit.c.expect(10 + 15)
    tester.circuit.done.expect(1)

    # tester.compile_and_run("verilator", magma_opts={"inline":True}, flags=["-Wno-fatal"])
    tester.compile_and_run("verilator", magma_opts={"inline":False}, flags=["-Wno-fatal"])

def test_hls_pipelined_mul():
    os.system("rm -f ./build")

    prog = HLSProg("pipelined_mul")
    a = prog.add_in("a", 32)
    b = prog.add_in("b", 32)
    f = prog.add_in("f", 32)
    o = prog.add_out("c", 32)

    res = prog.add_instr("umul_l1", [a, b])
    res.latency = 1
    s = prog.add_instr("uadd", [res, f])
    wr = prog.add_instr("write", [o, s])

    result = prog.compile();

    print('Result: ', result)

    tester = fault.Tester(result, result.CLK)

    tester.circuit.CLK = 0
    tester.circuit.RESET = 1
    tester.circuit.start = 0

    POSEDGE(tester)
    POSEDGE(tester)
    POSEDGE(tester)

    tester.circuit.done.expect(0)
    tester.circuit.a = 10
    tester.circuit.b = 15
    tester.circuit.f = 234
    tester.circuit.start = 1

    POSEDGE(tester)

    tester.circuit.done.expect(0);

    POSEDGE(tester)

    tester.circuit.c.expect(10 * 15 + 234)
    tester.circuit.done.expect(1)

    tester.compile_and_run("verilator", magma_opts={"inline":False}, flags=["-Wno-fatal"])

if __name__ == "__main__":
    test_hls()
    test_hls_add()
    test_hls_pipelined_mul()
