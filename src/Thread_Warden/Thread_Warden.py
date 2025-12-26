import threading
import time
import logging
from typing import Dict, Optional, Callable, Any, List, Set
from .Data_Containers import Restart_Policy, Thread_Entry
from .Interfaces import Child_Thread

# Configure a null handler so we don't spam if user hasn't configured logging
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Supervisor that monitors registered threads and restarts them if they fail to check in.
class Thread_Warden:
    def __init__(self):
        self._registry: Dict[str, Thread_Entry] = {}               # registry is the threads operating data
        self._restarting_threads: Set[str] = set()           # track threads currently being restarted
        self._lock = threading.RLock()                            # this prevents race conditions. stuff has to wait for the lock to unlock
        self._stop_event = threading.Event()                      # this is what stops the warden
        self._warden_thread: Optional[threading.Thread] = None   # this is the actual warden

    # Start the background monitoring thread - this is what monitor the child threads
    def start(self):
        with self._lock:
            if self._warden_thread and self._warden_thread.is_alive():
                return
            
            self._stop_event.clear()
            self._warden_thread = threading.Thread(
                target=self._monitor_loop, 
                name="Thread_Warden_Monitor", 
                daemon=True
            )
            self._warden_thread.start()
            logger.info("Thread_Warden started monitoring.")

    '''
    CALL THIS to register and start a new child thread.
    
    Args:
        name: Unique id for the thread
        factory: Function that creates the thread (accepts restored_state dict)
        policy: Restart behavior config
        timeout: Seconds without check_in before thread is considered dead (how long it can do a task before the warden kills it)
    '''
    def register_new_child(self, name: str, factory: Callable[[Optional[Dict[str, Any]]], Child_Thread], 
                           policy: Optional[Restart_Policy] = None, timeout: float = 30.0) -> None:

        with self._lock:
            if name in self._registry:
                raise ValueError(f"Thread '{name}' is already registered.")
            
            policy = policy or Restart_Policy() # use the preset policy or your custom policy
            
            # Create and start the thread
            try:
                thread = factory(None)   # no starting state
                if not isinstance(thread, Child_Thread):
                    raise TypeError("Factory must return a Child_Thread instance.")
                
                thread.name = name   # Ensure name is set on the thread object for debugging
                thread.start()
                
                entry = Thread_Entry(
                    instance=thread,
                    factory=factory,
                    policy=policy,
                    timeout=timeout,
                    last_seen=time.time(),
                    state={}
                )
                self._registry[name] = entry
                logger.info(f"Registered and started thread '{name}'.")
                
            except Exception as e:
                logger.error(f"Failed to start thread '{name}': {e}")
                raise

    # CALL THIS to tell warden the thread is alive and optionally save the threads state
    # CALL THIS before you have a thread do a danger task
    def check_in(self, name: str, state: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            if name not in self._registry:
                # If thread was removed or not registered, we ignore check-ins
                return

            entry = self._registry[name]
            
            # If the instance checking in is not the currently managed instance (zombie), ignore it
            # This handles cases where a zombie thread wakes up and tries to report
            current_thread = threading.current_thread()
            if isinstance(current_thread, Child_Thread) and entry.instance and current_thread is not entry.instance:
                # This might happen if we restarted it but the old one isn't dead yet
                return

            entry.last_seen = time.time()
            if state is not None:
                entry.state = state

            # Check if we should reset failure count (Stability check)
            # If it's been running successfully for long enough since the last restart
            time_since_restart = time.time() - entry.last_restart_time
            if entry.failure_count > 0 and time_since_restart > entry.policy.stable_threshold_seconds:
                logger.info(f"Thread '{name}' has been stable for {time_since_restart:.1f}s. Resetting failure count.")
                entry.failure_count = 0

    # Stop the warden and optionally stop all managed threads
    # it's possible the user wants to stop the warden but leave the child threads alive in some cases
    # very importent that you decide what to set stop_children as
    def shutdown(self, stop_children: bool = True) -> None:
        logger.info("Shutting down Thread_Warden...")
        self._stop_event.set()
        
        if self._warden_thread:
            self._warden_thread.join(timeout=2.0)
        
        if stop_children:
            threads_to_join = []

            with self._lock:
                for name, entry in self._registry.items():
                    logger.info(f"Stopping thread '{name}'...")
                    try:
                        entry.instance.stop()
                        threads_to_join.append(entry.instance)

                    except Exception as e:
                        logger.error(f"Error stopping thread '{name}': {e}")
            
            for t in threads_to_join:
                t.join(timeout=1.0)
                if t.is_alive():
                    logger.warning(f"Thread '{t.name}' did not stop gracefully.")

    # Background loop to check for stalled threads
    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            try:
                self._check_threads()
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")

    # Check all threads for timeouts and handle restarts
    def _check_threads(self) -> None:
        # Identification phase (Quick lock)
        now = time.time()
        crashed_names: List[str] = []
        
        with self._lock:
            for name, entry in self._registry.items():
                if self._is_thread_restarting(name):
                    continue

                if not entry.instance.is_alive():
                    logger.warning(f"Thread '{name}' found dead (not alive).")
                    crashed_names.append(name)
                elif (now - entry.last_seen) > entry.timeout:
                    logger.warning(f"Thread '{name}' timed out (last seen {now - entry.last_seen:.1f}s ago).")
                    crashed_names.append(name)

        # Recovery phase
        # We spawn a separate thread for each restart to avoid blocking the monitor loop
        # during the backoff sleep period
        for name in crashed_names:
            threading.Thread(target=self._perform_restart_sequence, args=(name,), daemon=True).start()

    # Helper to check if we should skip monitoring this entry because it's already restarting
    def _is_thread_restarting(self, name: str) -> bool:
        # This is called inside a lock
        return name in self._restarting_threads

    # Executes the full restart logic: Backoff -> Stop Old -> Start New
    def _perform_restart_sequence(self, name: str) -> None:
        backoff_time = 0.0
        
        with self._lock:
            if name not in self._registry:
                return
            entry = self._registry[name]
            
            # Double check to prevent race conditions
            if name in self._restarting_threads:
                 return # Already being restarted by another thread
            self._restarting_threads.add(name)
            
            backoff_time = entry.calculate_restart_backoff_time()
            entry.failure_count += 1
        
        try:
            logger.info(f"Restarting '{name}' in {backoff_time:.1f}s (Attempt #{entry.failure_count})")
            
            # Stop the old thread if it's still alive
            old_thread = None
            with self._lock:
                if name in self._registry:
                    old_thread = self._registry[name].instance
            
            if old_thread and old_thread.is_alive():
                old_thread.stop()
                try:
                    old_thread.join(timeout=2.0)
                except Exception:
                    pass

            # Wait (Backoff)
            if self._stop_event.wait(backoff_time):
                return # Shutting down

            # Create new thread
            with self._lock:
                if name not in self._registry:
                    return
                entry = self._registry[name]
                
                # Verify it hasn't recovered (ex: check_in update)
                # If last_seen is newer than when we started this restart sequence
                # The restart sequence started backoff_time ago.
                # If last_seen > now - backoff_time?
                # Actually, check_in updates last_seen
                if (time.time() - entry.last_seen) < entry.timeout and entry.instance.is_alive():
                     logger.info(f"Thread '{name}' seems to have recovered. Aborting restart.")
                     return

                try:
                    new_thread = entry.factory(entry.state)
                    if not isinstance(new_thread, Child_Thread):
                         logger.error(f"Factory for '{name}' returned invalid type.")
                         return

                    new_thread.name = name
                    new_thread.start()
                    
                    entry.instance = new_thread
                    entry.last_seen = time.time()
                    entry.last_restart_time = time.time()
                    logger.info(f"Successfully restarted '{name}'.")

                except Exception as e:
                    logger.error(f"Failed to factory/start replacement for '{name}': {e}")
                    # If we fail here, failure_count is already incremented.
                    # _restarting_threads will be cleared, so loop will retry later.
                    
        finally:
            with self._lock:
                self._restarting_threads.discard(name)
