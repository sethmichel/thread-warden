from dataclasses import dataclass
from typing import Dict, Any, Callable, Optional

# we use dataclass because these are basically just data containers
# reason we import Dict is for type hinting only. I think in python 3.9 you don't need to import it anymore

@dataclass
# how a thread should be restarted upon failure
class RestartPolicy:
    max_restarts_per_hour: int = 3
    restart_delay_seconds: float = 1.0
    enable_backoff: bool = True             # If true, delay doubles on consecutive failures
    stable_threshold_seconds: float = 60.0  # if a thread's been running this many seconds, reset its failure count

@dataclass
# Internal registry entry for a managed thread - basically all the threads data
class ThreadEntry:
    instance: Any                                      # Actually Child_Thread, typed as Any to avoid circular import
    factory: Callable[[Optional[Dict[str, Any]]], Any] # factory function for creating threads
                                                       #   Optional[Dict[str, Any]]] is the state. this is given to the replacement child thread
                                                       #   the last "Any" is the return value. returns a new thread instance
                                                       #   basically, this variable must be a function that accepts a state dictionary whenever we decide to run it
    policy: RestartPolicy
    timeout: float
    last_seen: float
    state: Optional[Dict[str, Any]] = None             # we give this to the factory. optional means it can be this or None
    failure_count: int = 0
    last_restart_time: float = 0.0

    # Calculate wait time based on failure count and policy
    def calcuate_restart_backoff_time(self) -> float:
        if not self.policy.enable_backoff:
            return self.policy.restart_delay_seconds
        
        # exponential backoff: base * (2 ^ failures)
        # e.g., 1s, 2s, 4s, 8s...
        # we cap this at x seconds, feel free to change this
        backoff_time = 2 ** self.failure_count
        if (backoff_time > 100):
            backoff_time = 100

        return self.policy.restart_delay_seconds * (backoff_time)

