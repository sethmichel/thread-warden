thread-warden
====

thread-warden is an open source helper for easily controlling your threads in multithreaded python programs.

It uses a "warden" thread which all other threads register with. The warden monitors the health of the other threads and if a thread stalls/dies/has an issue, then the warden can stop it and create another thread. Threads can check in with the warden and the warden will save their data and on starting a new thread - give the dead threads data to the new thread.

It has easily adjustable restart configs of the threads to handle cases where a thread will crash continuously; it waits longer and longer to restart the thread until it hits a restart limit. These limits and timers reset when the new thread has been alive for x seconds.

So it notes thread data states and can automatically restart threads in the same state while handling crash loops.

[![Tests](https://github.com/sethmichel/thread-warden/actions/workflows/Tests.yml/badge.svg?branch=main)](https://github.com/sethmichel/thread-warden/actions/workflows/Tests.yml)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)

![PyPI - Python Version](https://img.shields.io/badge/Python-3.8_|_3.9_|_3.10_|_3.11_|_3.12-blue)
![PyPI - Version](https://img.shields.io/badge/PyPi-v1.0-blue)

Why Use This?
====
Let's say you write a large python program using 4 concurrent threads all doing different tasks, including calling APIs. Those threads will inevitably have issues from time to time, especially if they run for hours at at time and especially any thread accessing a API.

This warden thread can automatically handle the health of your threads, and ensure you don't lose data on thread stalls. 

I personally had a program just like this example, and wrote this thread-warden for exactly this purpose. One of my threads would stall on API calls frequently and this would restart the thread, give it its old job again, and send it to the exact location in my code where it died to try the task again.

Installation
====

Install the package from PyPI:

```bash
pip install thread-warden
```

Quick Start
====

1. **Create a Thread Class**: thread-warden uses a `Child_Thread` class in Interfaces.py. You can use this class or make a custom class which inherits it. You should note step 4 which is that you'll need to call check_in() in your threads main loop.
2. **Define a Basic Factory**: Create a simple function that returns a new instance of your thread. This function will be saved in the data of each thread and the warden will use it to create a new thread.
3. **Register with Warden**: Start the warden and register your thread.
4. **Call the check_in() function**: Call this function anytime you want a thread to check in with the warden, and during the threads main loop. This will cause the warden to save the threads data and tell the warden it's still alive; it also resets the threads timeout so the warden doesn't stop it. You'll call this in your threads main loop, and may call it before any dangerous task that may kill the thread (like an API call).
5. **Optional: end the warden thread**: You can end the warden thread via  `warden.shutdown()`. This takes a bool parameter `stop_children` which defaults to True; it tells the warden if it should stop all child threads with it, or leave them all active.

The most difficult part of using this tool is making your own factory. You have to make the factory yourself because it lets you program your thread to do whatever you want, as opposed to this project making a default factory function.

```python
import time
import logging
from typing import Dict, Any, Optional
from Thread_Warden import Thread_Warden, Child_Thread

# 1. Subclass Child_Thread
class MyWorker(Child_Thread):
    def main_loop(self):
        while not self.stop_event.is_set():
            # Do work here
            print(f"{self.name} is working...")
            
            # Check in periodically to let Warden know we are alive
            # You can also save state here (e.g., current index, job ID)
            self.warden.check_in(self.name, state={"last_processed": time.time()})
            
            time.sleep(1)

# 2. Define a Factory Function
def worker_factory(saved_state: Optional[Dict[str, Any]]) -> Child_Thread:
    # If the thread crashed, 'saved_state' contains the last check_in data
    if saved_state:
        print(f"Restoring from state: {saved_state}")
    return MyWorker()

# 3. Setup Warden
if __name__ == "__main__":
    warden = Thread_Warden()
    warden.start()

    # Register the thread
    # timeout=5.0 means if the thread doesn't check_in() for 5 seconds, it gets restarted
    warden.register_new_child(
        name="Worker_1", 
        factory=worker_factory, 
        timeout=5.0
    )

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        warden.shutdown()
```