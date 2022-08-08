#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2022 F4PGA Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from colorama import Fore, Style

from f4pga.flows.common import deep, sfprint, bin_dir_path, share_dir_path, F4PGAException
from f4pga.flows.cache import F4Cache
from f4pga.flows.flow_config import FlowConfig
from f4pga.flows.runner import ModRunCtx, module_map, module_exec
from f4pga.flows.stage import Stage


class Flow:
    """Describes a complete, configured flow, ready for execution."""

    # Dependendecy to build
    target: str
    # Values in global scope
    cfg: FlowConfig
    # dependency-producer map
    os_map: "dict[str, Stage]"
    # Paths resolved for dependencies
    dep_paths: "dict[str, str | list[str]]"
    # Explicit configs for dependency paths
    # config_paths: 'dict[str, str | list[str]]'
    # Stages that need to be run
    run_stages: "set[str]"
    # Number of stages that relied on outdated version of a (checked) dependency
    deps_rebuilds: "dict[str, int]"
    f4cache: "F4Cache | None"
    flow_cfg: FlowConfig

    def __init__(self, target: str, cfg: FlowConfig, f4cache: "F4Cache | None"):
        self.target = target

        # Associate a stage with every possible output.
        # This is commonly refferef to as `os_map` (output-stage-map) through the code.
        os_map: "dict[str, Stage]" = {}  # Output-Stage map
        for stage in cfg.stages.values():
            for output in stage.produces:
                if not os_map.get(output.name):
                    os_map[output.name] = stage
                elif os_map[output.name] != stage:
                    raise Exception(
                        f"Dependency `{output.name}` is generated by "
                        f"stage `{os_map[output.name].name}` and "
                        f"`{stage.name}`. Dependencies can have only one "
                        "provider at most."
                    )
        self.os_map = os_map

        self.dep_paths = {
            n: p
            for n, p in cfg.get_dependency_overrides().items()
            if (p is not None) and p_req_exists(p)  # and not p_dep_differ(p, f4cache)
        }
        if f4cache is not None:
            for dep in self.dep_paths.values():
                self._cache_deps(dep, f4cache)

        self.run_stages = set()
        self.f4cache = f4cache
        self.cfg = cfg
        self.deps_rebuilds = {}

        self._resolve_dependencies(self.target, set())

    @staticmethod
    def _config_mod_runctx(
        stage: Stage,
        values: "dict[str, ]",
        dep_paths: "dict[str, str | list[str]]",
        config_paths: "dict[str, str | list[str]]",
    ):
        takes = {}
        for take in stage.takes:
            paths = dep_paths.get(take.name)
            if paths:  # Some takes may be not required
                takes[take.name] = paths

        produces = {}
        for prod in stage.produces:
            if dep_paths.get(prod.name):
                produces[prod.name] = dep_paths[prod.name]
            elif config_paths.get(prod.name):
                produces[prod.name] = config_paths[prod.name]

        return ModRunCtx(share_dir_path, bin_dir_path, {"takes": takes, "produces": produces, "values": values})

    @staticmethod
    def _cache_deps(path: str, f4cache: F4Cache):
        def _process_dep_path(path: str, f4cache: F4Cache):
            f4cache.process_file(Path(path))

        deep(_process_dep_path)(path, f4cache)

    def _dep_will_differ(self, dep: str, paths, consumer: str):
        """
        Check if a dependency or any of the dependencies it depends on differ from
        their last versions.
        """
        if not self.f4cache:  # Handle --nocache mode
            return True
        provider = self.os_map.get(dep)
        if provider and (provider.name in self.run_stages):
            return True
        return p_dep_differ(paths, consumer, self.f4cache)

    def _resolve_dependencies(self, dep: str, stages_checked: "set[str]", skip_dep_warnings: "set[str]" = None):
        if skip_dep_warnings is None:
            skip_dep_warnings = set()

        # Initialize the dependency status if necessary
        if self.deps_rebuilds.get(dep) is None:
            self.deps_rebuilds[dep] = 0
        # Check if an explicit dependency is already resolved
        paths = self.dep_paths.get(dep)
        if paths and not self.os_map.get(dep):
            return
        # Check if a stage can provide the required dependency
        provider = self.os_map.get(dep)
        if not provider or provider.name in stages_checked:
            return

        # TODO: Check if the dependency is "on-demand" and force it in provider's
        # config if it is.

        for take in provider.takes:
            self._resolve_dependencies(take.name, stages_checked, skip_dep_warnings)
            # If any of the required dependencies is unavailable, then the
            # provider stage cannot be run
            take_paths = self.dep_paths.get(take.name)
            # Add input path to values (dirty hack)
            provider.value_overrides[f":{take.name}"] = take_paths

            if not take_paths and take.spec == "req":
                sfprint(
                    0,
                    f"    Stage `{Style.BRIGHT + provider.name + Style.RESET_ALL}` is "
                    f"unreachable due to unmet dependency `{Style.BRIGHT + take.name + Style.RESET_ALL}`",
                )
                return

            will_differ = False
            if take_paths is None:
                # TODO: This won't trigger rebuild if an optional dependency got removed
                will_differ = False
            elif p_req_exists(take_paths):
                will_differ = self._dep_will_differ(take.name, take_paths, provider.name)
            else:
                will_differ = True

            if will_differ:
                if take.name not in skip_dep_warnings:
                    sfprint(
                        2,
                        f"{Style.BRIGHT}{take.name}{Style.RESET_ALL} is causing "
                        f"rebuild for `{Style.BRIGHT}{provider.name}{Style.RESET_ALL}`",
                    )
                    skip_dep_warnings.add(take.name)
                self.run_stages.add(provider.name)
                self.deps_rebuilds[take.name] += 1

        outputs = module_map(
            provider.module,
            self._config_mod_runctx(
                provider, self.cfg.get_r_env(provider.name).values, self.dep_paths, self.cfg.get_dependency_overrides()
            ),
        )
        for output_paths in outputs.values():
            if output_paths is not None:
                if p_req_exists(output_paths) and self.f4cache:
                    self._cache_deps(output_paths, self.f4cache)

        stages_checked.add(provider.name)
        self.dep_paths.update(outputs)

        for _, out_paths in outputs.items():
            if (out_paths is not None) and not (p_req_exists(out_paths)):
                self.run_stages.add(provider.name)

        # Verify module's outputs and add paths as values.
        outs = outputs.keys()
        for o in provider.produces:
            if o.name not in outs:
                if o.spec == "req" or (o.spec == "demand" and o.name in self.cfg.get_dependency_overrides().keys()):
                    fatal(-1, f"Module {provider.name} did not produce a mapping " f"for a required output `{o.name}`")
                else:
                    # Remove an on-demand/optional output that is not produced
                    # from os_map.
                    self.os_map.pop(o.name)
            # Add a value for the output (dirty ack yet again)
            o_path = outputs.get(o.name)

            if o_path is not None:
                provider.value_overrides[f":{o.name}"] = outputs.get(o.name)

    def print_resolved_dependencies(self, verbosity: int):
        deps = list(self.deps_rebuilds.keys())
        deps.sort()

        for dep in deps:
            status = Fore.RED + "[X]" + Fore.RESET
            source = Fore.YELLOW + "MISSING" + Fore.RESET
            paths = self.dep_paths.get(dep)

            if paths:
                exists = p_req_exists(paths)
                provider = self.os_map.get(dep)
                if provider and provider.name in self.run_stages:
                    status = Fore.YELLOW + ("[R]" if exists else "[S]") + Fore.RESET
                    source = f"{Fore.BLUE + self.os_map[dep].name + Fore.RESET} -> {paths}"
                elif exists:
                    status = Fore.GREEN + ("[N]" if self.deps_rebuilds[dep] > 0 else "[O]") + Fore.RESET
                    source = paths
            elif self.os_map.get(dep):
                status = Fore.RED + "[U]" + Fore.RESET
                source = f"{Fore.BLUE + self.os_map[dep].name + Fore.RESET} -> ???"

            sfprint(verbosity, f"    {Style.BRIGHT + status} " f"{dep + Style.RESET_ALL}:  {source}")

    def _build_dep(self, dep):
        paths = self.dep_paths.get(dep)
        if not paths:
            sfprint(2, f"Dependency {dep} is unresolved.")
            return False

        provider = self.os_map.get(dep)
        run = (provider.name in self.run_stages) if provider else False

        if p_req_exists(paths) and not run:
            return True
        else:
            assert provider

            any_dep_differ = False if (self.f4cache is not None) else True
            for p_dep in provider.takes:
                if not self._build_dep(p_dep.name):
                    assert p_dep.spec != "req"
                    continue
                if self.f4cache is not None:
                    any_dep_differ |= p_update_dep_statuses(self.dep_paths[p_dep.name], provider.name, self.f4cache)

            # If dependencies remained the same, consider the dep as up-to date
            # For example, when changing a comment in Verilog source code,
            # the initial dependency resolution will report a need for complete
            # rebuild, however, after the synthesis stage, the generated eblif
            # will reamin the same, thus making it unnecessary to continue the
            # rebuild process.
            if (not any_dep_differ) and p_req_exists(paths):
                sfprint(
                    2,
                    f"Skipping rebuild of `"
                    f"{Style.BRIGHT + dep + Style.RESET_ALL}` because all "
                    f"of it's dependencies remained unchanged",
                )
                return True

            module_exec(
                provider.module,
                self._config_mod_runctx(
                    provider,
                    self.cfg.get_r_env(provider.name).values,
                    self.dep_paths,
                    self.cfg.get_dependency_overrides(),
                ),
            )

            self.run_stages.discard(provider.name)

            for product in provider.produces:
                if (product.spec == "req") and not p_req_exists(paths):
                    raise DependencyNotProducedException(dep, provider.name)
                prod_paths = self.dep_paths[product.name]
                if (prod_paths is not None) and p_req_exists(paths) and self.f4cache:
                    self._cache_deps(prod_paths, self.f4cache)

        return True

    def execute(self):
        self._build_dep(self.target)
        if self.f4cache:
            self._cache_deps(self.dep_paths[self.target], self.f4cache)
            p_update_dep_statuses(self.dep_paths[self.target], "__target", self.f4cache)
        sfprint(0, f"Target {Style.BRIGHT + self.target + Style.RESET_ALL} -> {self.dep_paths[self.target]}")


class DependencyNotProducedException(F4PGAException):
    dep_name: str
    provider: str

    def __init__(self, dep_name: str, provider: str):
        self.dep_name = dep_name
        self.provider = provider
        self.message = f"Stage `{self.provider}` did not produce promised dependency `{self.dep_name}`"


def p_req_exists(r):
    """
    Checks whether a dependency exists on a drive.
    """
    if type(r) is str:
        if not Path(r).exists():
            return False
    elif type(r) is list:
        return not (False in map(p_req_exists, r))
    else:
        raise Exception(f"Requirements can be currently checked only for single paths, or path lists (reason: {r})")
    return True


def p_update_dep_statuses(paths, consumer: str, f4cache: F4Cache):
    if type(paths) is str:
        return f4cache.update(Path(paths), consumer)
    elif type(paths) is list:
        for p in paths:
            return p_update_dep_statuses(p, consumer, f4cache)
    elif type(paths) is dict:
        for _, p in paths.items():
            return p_update_dep_statuses(p, consumer, f4cache)
    fatal(-1, "WRONG PATHS TYPE")


def p_dep_differ(paths, consumer: str, f4cache: F4Cache):
    """
    Check if a dependency differs from its last version, lack of dependency is treated as "differs".
    """
    if type(paths) is str:
        if not Path(paths).exists():
            return True
        return f4cache.get_status(paths, consumer) != "same"
    elif type(paths) is list:
        return True in [p_dep_differ(p, consumer, f4cache) for p in paths]
    elif type(paths) is dict:
        return True in [p_dep_differ(p, consumer, f4cache) for _, p in paths.items()]
    return False
