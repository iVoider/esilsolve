import z3
from .r2api import R2API
from . import esilops
from .esilclasses import * 
from .esilstate import *

# some constants for exec type idk
UNCON   = 0
IF      = 1 
ELSE    = 2
EXEC    = 3
NO_EXEC = 4

IF_C     = "?{"
ELSE_C   = "}{"
ENDIF_C  = "}"
GOTO_C   = "GOTO"
REPEAT_C = "REPEAT"
BREAK_C  = "BREAK"

class ESILProcess:
    """ 
    Executes ESIL expressions and handles results

    >>> state.proc.parse_expression("4,rax,rbx,=", state)
    """

    def __init__(self, r2p: R2API = None, **kwargs):
        self.kwargs = kwargs
        self.debug = kwargs.get("debug", False)
        self.trace = kwargs.get("trace", False)
        self.bail = kwargs.get("bail", False)
        self.check_perms = kwargs.get("check", False)

        self.tactics = self.get_boolref_tactics()

        if r2p == None:
            r2api = R2API()
        else:
            r2api = r2p

        self.r2api = r2api
        self.info = self.r2api.get_info()

        # this depth limit is maybe already too high
        # condition "size" scales like 2**limit
        self.goto_depth_limit = 32
        self.lazy = kwargs.get("lazy", False)

        # TODO handle this stuff
        self.traps = {}
        self.syscalls = {}

        # try to init vexit, an optional module that uses vex
        # if an esil expression is not available
        # its not pretty but it (sort of) works (sometimes)
        self.do_vexit = kwargs.get("vexit", False)
        if self.do_vexit:
            try:
                from .vexit import VexIt
                self.vexit = VexIt(
                    self.info["info"]["arch"], 
                    self.info["info"]["bits"])
            except:
                self.vexit = None
    
    def execute_instruction(self, state, instr: Dict):
        if self.bail and "esil" not in instr:
            raise ESILUnimplementedException("no esil for: %s" % str(instr))

        offset = instr["offset"]

        if self.check_perms:
            state.memory.check(offset, "x")
            
        if self.debug:
            print("\nexpr: %s" % instr.get("esil", "no esil expr"))
            print("%016x: %s" % (offset, instr["opcode"]))

        # old pc should never be anything other than a BitVecVal  
        old_pc = offset + instr["size"]

        state.registers["PC"] = old_pc

        esil = instr.get("esil", ",")
        if type(esil) == str:
            esil = esil.split(",")
            instr["esil"] = esil

        if self.do_vexit and self.vexit != None:
            if esil in ("", "TODO") and instr["type"] != "nop":
                esil = self.vexit.convert(instr)

        self.parse_expression(esil, state) 

        state.steps += 1
        states = []

        pc = state.registers["PC"]
        if z3.is_bv_value(pc):
            if self.trace:
                self.r2api.emustep()
                self.trace_registers(state)

            states.append(state)
        else:
            # symbolic pc value
            if self.debug:
                print("symbolic pc: %s" % str(pc))

            possible_pcs = []

            # if lazy don't eval, just try both If addresses
            if self.lazy and pc.decl().name() == "if":
                possible_pcs = self.get_lazy_pcs(pc)
            
            if possible_pcs == []:
                possible_pcs = state.eval_max(pc)

                if possible_pcs == [] and pc.decl().name() == "if":
                    # if its still [] we prolly timed out
                    # treat it like a lazy solve maybe
                    possible_pcs = self.get_lazy_pcs(pc)[:1]

            for possible_pc in possible_pcs:

                # this is the secret to speed, dont always clone states
                if len(possible_pcs) > 1:
                    new_state = state.clone()
                else:
                    new_state = state

                new_state.constrain(self.eq(pc == possible_pc))
                new_state.registers["PC"] = possible_pc

                states.append(new_state)

        return states

    def get_lazy_pcs(self, pc):
        arg1 = z3.simplify(pc.arg(1))
        arg2 = z3.simplify(pc.arg(2))

        if z3.is_bv_value(arg1) and z3.is_bv_value(arg2):
            return [arg1.as_long(), arg2.as_long()]
        else:
            return []

    def parse_expression(self, expression, state):

        temp_stack1 = []
        temp_stack2 = []
        exec_type = UNCON

        if type(expression) == str:
            words = expression.split(",")
        else:
            words = expression

        word_ind = 0
        words_len = len(words)

        goto = None
        goto_condition = None
        break_condition = None
        goto_depth = 0
        repeat = None
        
        while word_ind < words_len:
            #print(words[word_ind], temp_stack1, state.stack)
            #print(state.condition)
            word = words[word_ind]

            if word == IF_C:
                words[word_ind] = IF_C

                state.condition = self.do_if(state)
                if type(state.condition) == bool:
                    if state.condition == True:
                        exec_type = EXEC
                    else:
                        exec_type = NO_EXEC

                    state.condition = None
                else:
                    exec_type = IF
                    temp_stack1 = state.stack
                    state.stack = temp_stack1[:]

            elif word == ELSE_C:
                words[word_ind] = ELSE_C

                if exec_type == NO_EXEC:
                    exec_type = EXEC
                elif exec_type == EXEC:
                    exec_type = NO_EXEC
                else:
                    state.condition = z3.Not(state.condition)
                    exec_type = ELSE
                    temp_stack2 = state.stack
                    state.stack = temp_stack1[:]

            elif word == ENDIF_C:
                words[word_ind] = ENDIF_C

                if NO_EXEC != exec_type != EXEC:
                    new_stack = []
                    new_temp = temp_stack1
                    if exec_type == ELSE:
                        new_temp = temp_stack2

                    while state.stack != [] and new_temp != []:
                        else_val, = esilops.pop_values(new_temp, state)
                        if_val, = esilops.pop_values(state.stack, state)
                        condval = z3.If(state.condition, if_val, else_val)
                        new_stack.append(z3.simplify(condval))

                    if break_condition == None:
                        state.condition = None
                    else:
                        # if there is a break condition 
                        # we need to restore that
                        state.condition = z3.Not(break_condition)

                    new_stack.reverse()
                    state.stack = new_stack

                exec_type = UNCON

                if goto != None and exec_type != NO_EXEC:
                    word_ind = goto-1
                    state.condition = goto_condition
                    goto = None

            elif exec_type != NO_EXEC and word == GOTO_C:
                words[word_ind] = GOTO_C

                # goto makes things a bit wild
                goto, = esilops.pop_values(state.stack, state)

                if z3.is_bv_value(goto):
                    goto = goto.as_long()
                else: # hopefully this doesn't happen
                    goto = state.evalcon(goto).as_long()

                goto_depth += 1

                if state.condition != None and goto_depth > self.goto_depth_limit:
                    # constrain the current condition to not be true
                    # effectively cutting off the nested gotos
                    state.constrain(z3.Not(state.condition))
                    goto = None
            
                elif self.check_condition(state.condition, state):
                    goto_condition = state.condition

                    if exec_type == UNCON:
                        word_ind = goto-1
                        goto = None
                    
                else:
                    goto = None

            elif exec_type != NO_EXEC and word == REPEAT_C:
                words[word_ind] = REPEAT_C
                # REPEAT is barely used and the code looks wrong
                # but this is here for completeness sake

                if repeat == None or repeat > 1:
                    go, rep = esilops.pop_values(state.stack, state)

                    if repeat == None:
                        repeat = rep

                    repeat -= 1
                    state.stack.append(repeat)
                    word_ind = go-1

                else:
                    repeat = None

            elif exec_type != NO_EXEC and word == BREAK_C:
                words[word_ind] = BREAK_C

                # if its unconstrained just break
                if exec_type in (UNCON, EXEC):
                    break
                elif self.check_condition(state.condition, state):
                    # otherwise uhhh idk for now
                    break_condition = state.condition

            elif exec_type != NO_EXEC:

                if type(word) == int or word in state.registers:
                    state.stack.append(word)

                elif word in esilops.opcodes:
                    op = esilops.opcodes[word]
                    op(word, state.stack, state)

                else:
                    val = self.get_push_value(word)
                    state.stack.append(val)
                    words[word_ind] = val

            word_ind += 1

        state.condition = None

    def get_push_value(self, word):
        if(word.isdigit()):
            return int(word)
        elif word[:1] == "-" and word[1:].isdigit():
            return int(word)
        elif word[:2] == "0x" or word[:3] == "-0x":
            return int(word, 16)
        elif "." in word:
            return float(word)
        else:
            return word

    def do_if(self, state):
        val, = esilops.pop_values(state.stack, state)
        val = z3.simplify(val)

        zero = 0
        if z3.is_bv_value(val):
            val = val.as_long()
                
        elif z3.is_bv(val):
            zero = z3.BitVecVal(0, val.size())

        if state.condition == None:
            if type(val) == int:
                return val != zero
            else:
                #return val != zero
                return self.eq(val != zero)
        else:
            return z3.And(self.eq(val != zero), state.condition)

    def check_condition(self, condition, state):
        if condition == None:
            return True

        state.solver.push()
        state.solver.add(condition)
        is_sat = state.is_sat()
        state.solver.pop()

        return is_sat

    def eq(self, expr):
        return self.tactics(expr).as_expr()

    # stolen mostly from angr 
    # but slightly different / better
    def get_boolref_tactics(self):
        tactics = z3.Then(
            z3.Tactic("simplify"),
            #z3.Tactic("sat-preprocess"), # uh this makes things wrong? 
            z3.Tactic("cofactor-term-ite"),
            z3.Tactic("propagate-ineqs"),
            z3.Tactic("propagate-values"),
            z3.Tactic("unit-subsume-simplify"),
            z3.Tactic("aig"),
        )

        return tactics

    def trace_registers(self, state):
        for regname in state.registers._registers:
            register = state.registers._registers[regname]
            #print(regname, reg_value)
            if register["type_str"] in ("gpr", "flg"):
                emureg = self.r2api.get_reg_value(register["name"])
                try:
                    reg_value = z3.simplify(state.registers[regname])
                    #print(reg_value)
                    if reg_value.as_long() != emureg:
                        print("%s: %s , %s" % (register["name"], reg_value, emureg))
                except Exception as e:
                    #print(e)
                    pass

    def clone(self):
        clone = self.__class__(self.r2api, debug=self.debug, trace=self.trace)
        return clone