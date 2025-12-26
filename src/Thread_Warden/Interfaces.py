import threading
from abc import ABC
from typing import Optional, Dict, Any

# abc is a python library for abstract base classes

'''
Abstract base class for threads managed by the Thread_Warden

Subclasses must:
    1. Accept restored_state in __init__
    2. Periodically check self.is_stopped() in run()
'''
class Child_Thread(threading.Thread, ABC):
    def __init__(self, restored_state: Optional[Dict[str, Any]] = None, name: Optional[str] = None, daemon: bool = True):
        super().__init__(name=name, daemon=daemon)
        self._stop_event = threading.Event()
        self.restored_state = restored_state

    # tell thread to stop gracefully
    def stop(self):
        self._stop_event.set()

    # Check if the thread has been told to stop
    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

