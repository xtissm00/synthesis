import stormpy

from .property import *

from collections import OrderedDict
import random

class MarkovChain:

    # options for the construction of chains
    builder_options = None
    # model checking environment (method & precision)
    environment = None

    @classmethod
    def initialize(cls, specification):
        # builder options
        formulae = specification.stormpy_formulae()
        cls.builder_options = stormpy.BuilderOptions(formulae)
        cls.builder_options.set_build_with_choice_origins(True)
        cls.builder_options.set_build_state_valuations(True)
        cls.builder_options.set_add_overlapping_guards_label()
    
        # model checking environment
        cls.environment = stormpy.Environment()
        
        se = cls.environment.solver_environment

        se.set_linear_equation_solver_type(stormpy.EquationSolverType.gmmxx)
        # se.minmax_solver_environment.precision = stormpy.Rational(Property.mc_precision)
        # se.minmax_solver_environment.method = stormpy.MinMaxMethod.policy_iteration
        se.minmax_solver_environment.method = stormpy.MinMaxMethod.value_iteration
        # se.minmax_solver_environment.method = stormpy.MinMaxMethod.sound_value_iteration
        # se.minmax_solver_environment.method = stormpy.MinMaxMethod.interval_iteration
        # se.minmax_solver_environment.method = stormpy.MinMaxMethod.optimistic_value_iteration
        # se.minmax_solver_environment.method = stormpy.MinMaxMethod.topological

    
    @classmethod
    def from_prism(self, prism):
        if prism.model_type in [stormpy.storage.PrismModelType.MDP, stormpy.storage.PrismModelType.POMDP]:
            # TODO why do we disable choice labels here?
            MarkovChain.builder_options.set_build_choice_labels(True)
            model = stormpy.build_sparse_model_with_options(prism, MarkovChain.builder_options)
            MarkovChain.builder_options.set_build_choice_labels(False)
        if prism.model_type == stormpy.storage.PrismModelType.MA:
            model = stormpy.build_sparse_model_with_options(prism, MarkovChain.builder_options)

        og = model.labeling.get_states("overlap_guards").number_of_set_bits()
        assert og == 0, "PRISM program contains overlapping guards"

        return model

    
    def __init__(self, model, quotient_container, quotient_state_map, quotient_choice_map):
        if model.labeling.contains_label("overlap_guards"):
            assert model.labeling.get_states("overlap_guards").number_of_set_bits() == 0
        self.model = model
        self.quotient_container = quotient_container
        self.quotient_choice_map = quotient_choice_map
        self.quotient_state_map = quotient_state_map

        # identify simple holes
        design_space = quotient_container.design_space
        hole_to_states = [0 for hole in design_space]
        for state in range(self.states):
            for hole in quotient_container.coloring.state_to_holes[self.quotient_state_map[state]]:
                hole_to_states[hole] += 1
        self.hole_simple = [hole_to_states[hole] <= 1 for hole in design_space.hole_indices]

        self.analysis_hints = None
    
    @property
    def states(self):
        return self.model.nr_states

    @property
    def choices(self):
        return self.model.nr_choices

    @property
    def is_dtmc(self):
        return self.model.nr_choices == self.model.nr_states

    @property
    def initial_state(self):
        return self.model.initial_states[0]

    def model_check_formula(self, formula):
        result = stormpy.model_checking(
            self.model, formula, only_initial_states=False,
            extract_scheduler=(not self.is_dtmc),
            # extract_scheduler=True,
            environment=self.environment
        )
        assert result is not None
        return result

    def model_check_formula_hint(self, formula, hint):
        stormpy.synthesis.set_loglevel_off()
        task = stormpy.core.CheckTask(formula, only_initial_states=False)
        task.set_produce_schedulers(produce_schedulers=True)
        result = stormpy.synthesis.model_check_with_hint(self.model, task, self.environment, hint)
        return result

    def model_check_property(self, prop, alt = False):
        direction = "prim" if not alt else "seco"
        # get hint
        hint = None
        if self.analysis_hints is not None:
            hint_prim,hint_seco = self.analysis_hints[prop]
            hint = hint_prim if not alt else hint_seco
            # hint = self.analysis_hints[prop]

        formula = prop.formula if not alt else prop.formula_alt
        if hint is None:
            result = self.model_check_formula(formula)
        else:
            result = self.model_check_formula_hint(formula, hint)
        
        value = result.at(self.initial_state)

        return PropertyResult(prop, result, value)


class DTMC(MarkovChain):

    def check_constraints(self, properties, property_indices = None, short_evaluation = False):
        '''
        Check constraints.
        :param properties a list of all constraints
        :param property_indices a selection of property indices to investigate
        :param short_evaluation if set to True, then evaluation terminates as
          soon as a constraint is not satisfied
        '''

        # implicitly, check all constraints
        if property_indices is None:
            property_indices = [index for index,_ in enumerate(properties)]
        
        # check selected properties
        results = [None for prop in properties]
        for index in property_indices:
            prop = properties[index]
            result = self.model_check_property(prop)
            results[index] = result
            if short_evaluation and not result.sat:
                break

        return ConstraintsResult(results)

    def check_specification(self, specification, property_indices = None, short_evaluation = False):
        constraints_result = self.check_constraints(specification.constraints, property_indices, short_evaluation)
        optimality_result = None
        if specification.has_optimality and not (short_evaluation and not constraints_result.all_sat):
            optimality_result = self.model_check_property(specification.optimality)
        return SpecificationResult(constraints_result, optimality_result)



class MDP(MarkovChain):

    # whether the secondary direction will be explored if primary is not enough
    compute_secondary_direction = False

    def __init__(self, model, quotient_container, quotient_state_map, quotient_choice_map, design_space):
        super().__init__(model, quotient_container, quotient_state_map, quotient_choice_map)

        self.design_space = design_space
        self.analysis_hints = None
        self.quotient_to_restricted_action_map = None


    def check_property(self, prop):

        # check primary direction
        primary = self.model_check_property(prop, alt = False)
        
        # no need to check secondary direction if primary direction yields UNSAT
        if not primary.sat:
            return MdpPropertyResult(prop, primary, None, False, None, None, None, None)

        # primary direction is SAT
        # check if the primary scheduler is consistent
        selection,choice_values,expected_visits,scores,consistent = self.quotient_container.scheduler_consistent(self, prop, primary.result)    
        
        # regardless of whether it is consistent or not, we compute secondary direction to show that all SAT

        # compute secondary direction
        secondary = self.model_check_property(prop, alt = True)
        if self.is_dtmc and primary.value != secondary.value:
            dtmc = self.quotient_container.mdp_to_dtmc(self.model)
            result = stormpy.model_checking(
                dtmc, prop.formula, only_initial_states=False,
                extract_scheduler=(not self.is_dtmc),
                # extract_scheduler=True,
                environment=self.environment
            )

        feasibility = True if secondary.sat else None
        return MdpPropertyResult(prop, primary, secondary, feasibility, selection, choice_values, expected_visits, scores)

    def check_constraints(self, properties, property_indices = None, short_evaluation = False):
        if property_indices is None:
            property_indices = [index for index,_ in enumerate(properties)]

        results = [None for prop in properties]
        for index in property_indices:
            prop = properties[index]
            result = self.check_property(prop)
            results[index] = result
            if short_evaluation and result.feasibility == False:
                break

        return MdpConstraintsResult(results)

    def check_optimality(self, prop):
        # check primary direction
        primary = self.model_check_property(prop, alt = False)

        if not primary.improves_optimum:
            # OPT <= LB
            return MdpOptimalityResult(prop, primary, None, None, None, False, None, None, None, None)

        # LB < OPT
        # check if LB is tight
        selection,choice_values,expected_visits,scores,consistent = self.quotient_container.scheduler_consistent(self, prop, primary.result)
        if consistent:
            # LB is tight and LB < OPT
            scheduler_assignment = self.design_space.copy()
            scheduler_assignment.assume_options(selection)
            improving_assignment, improving_value = self.quotient_container.double_check_assignment(scheduler_assignment)
            return MdpOptimalityResult(prop, primary, None, improving_assignment, improving_value, False, selection, choice_values, expected_visits, scores)

        if not MDP.compute_secondary_direction:
            return MdpOptimalityResult(prop, primary, None, None, None, True, selection, choice_values, expected_visits, scores)

        # UB might improve the optimum
        secondary = self.model_check_property(prop, alt = True)

        if not secondary.improves_optimum:
            # LB < OPT < UB :  T < LB < OPT < UB (can improve) or LB < T < OPT < UB (cannot improve)
            can_improve = primary.sat
            return MdpOptimalityResult(prop, primary, secondary, None, None, can_improve, selection, choice_values, expected_visits, scores)

        # LB < UB < OPT
        # this family definitely improves the optimum
        assignment = self.design_space.pick_any()
        improving_assignment, improving_value = self.quotient_container.double_check_assignment(assignment, prop)
        # either LB < T, LB < UB < OPT (can improve) or T < LB < UB < OPT (cannot improve)
        can_improve = primary.sat
        return MdpOptimalityResult(prop, primary, secondary, improving_assignment, improving_value, can_improve, selection, choice_values, scores)


    def check_specification(self, specification, property_indices = None, short_evaluation = False):
        constraints_result = self.check_constraints(specification.constraints, property_indices, short_evaluation)
        optimality_result = None
        if specification.has_optimality and not (short_evaluation and constraints_result.feasibility == False):
            optimality_result = self.check_optimality(specification.optimality)
        return SpecificationResult(constraints_result, optimality_result)


    def prepare_sampling(self):
        tm = self.model.transition_matrix
        state_row_group = []
        for state in range(self.states):
            row_group = []
            num_actions = self.model.get_nr_available_actions(state)
            for row_index in range(tm.get_row_group_start(state),tm.get_row_group_end(state)):
                columns = [entry.column for entry in tm.get_row(row_index)]
                values = [entry.value() for entry in tm.get_row(row_index)]
                row_group.append( (columns,values) )
            state_row_group.append(row_group)
        return state_row_group

    
    def random_action(self, state):
        num_actions = self.model.get_nr_available_actions(state)
        action = random.randint(0,num_actions-1)
        return action

    def random_transition(self, state, action, state_row_group=None):

        if state_row_group is not None:
            succs,probs = state_row_group[state][action]
            res = random.choices(succs, probs)
            return res[0]


        # get row
        tm = self.model.transition_matrix
        row_index = tm.get_row_group_start(state) + action
        row = tm.get_row(row_index)

        # pack successor and probabilities
        succs = []
        probs = []
        for entry in row:
            succs.append(entry.column)
            probs.append(entry.value())

        # sample a successor
        res = random.choices(succs, probs)
        return res[0]

    
    def random_path(self, length, state_row_group=None):
        path = []
        state = self.initial_state
        while length > 0:
            action = self.random_action(state)
            path.append((state,action))
            length -= 1
            state = self.random_transition(state,action,state_row_group)
        return path

    def evaluate_path(self, path, reward_name):

        # assuming state reward
        reward_model = self.model.get_reward_model(reward_name)
        assert reward_model.state_rewards

        reward_sum = 0
        for state,_ in path:
            reward = reward_model.get_state_reward(state)
            reward_sum += reward
        return reward_sum