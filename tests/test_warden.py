import time
import pytest
from Thread_Warden import Thread_Warden, Child_Thread, Restart_Policy

# --- Mock Thread for Testing ---
class MockWorkerThread(Child_Thread):
    def __init__(self, restored_state=None, name=None, daemon=True):
        super().__init__(restored_state, name, daemon)
        self.state = restored_state or {"count": 0}
        self.work_done = 0

    def run(self):
        while not self.is_stopped():
            self.work_done += 1
            time.sleep(0.1)

# --- Factory Helper ---
def mock_thread_factory(state):
    return MockWorkerThread(restored_state=state)

# --- Tests ---

def test_registration_and_start():
    """Test that we can register a thread and it actually starts running."""
    warden = Thread_Warden()
    warden.start()
    
    try:
        warden.register_new_child("worker-1", mock_thread_factory, timeout=5.0)
        
        # Give it a moment to start
        time.sleep(0.5)
        
        # Verify it's in the registry and running
        assert "worker-1" in warden._registry
        entry = warden._registry["worker-1"]
        assert entry.instance.is_alive()
        assert isinstance(entry.instance, MockWorkerThread)
        assert entry.instance.work_done > 0
        
    finally:
        warden.shutdown()

def test_restart_on_crash():
    """Test that if a thread dies (stops alive), the warden restarts it."""
    warden = Thread_Warden()
    warden.start()
    
    # Fast restart policy for testing
    fast_policy = Restart_Policy(restart_delay_seconds=0.1, enable_backoff=False)
    
    try:
        warden.register_new_child("crasher-1", mock_thread_factory, policy=fast_policy, timeout=5.0)
        time.sleep(0.5)
        
        # Get the original thread instance
        original_thread = warden._registry["crasher-1"].instance
        
        # Simulate a crash by stopping it manually
        original_thread.stop() # Graceful stop, but effectively "dead" to the warden if not joined properly?
        # Actually Child_Thread.stop just sets the event. The loop in MockWorkerThread will exit.
        # So the thread will finish execution and be not alive.
        
        # Wait for the loop to exit
        original_thread.join(timeout=2.0)
        assert not original_thread.is_alive()
        
        # Wait for warden to detect and restart (monitor loop sleeps 1.0s, restart takes a bit)
        time.sleep(2.5) 
        
        # Check that a NEW thread is now running
        new_entry = warden._registry["crasher-1"]
        assert new_entry.instance is not original_thread
        assert new_entry.instance.is_alive()
        assert new_entry.failure_count == 1
        
    finally:
        warden.shutdown()

def test_state_restoration():
    """Test that state is preserved across restarts."""
    warden = Thread_Warden()
    warden.start()
    fast_policy = Restart_Policy(restart_delay_seconds=0.1, enable_backoff=False)
    
    try:
        warden.register_new_child("stateful-1", mock_thread_factory, policy=fast_policy, timeout=5.0)
        time.sleep(0.2)
        
        # Simulate the thread doing work and checking in state
        important_data = {"count": 42, "status": "processing"}
        warden.check_in("stateful-1", state=important_data)
        
        # Kill the thread
        original_thread = warden._registry["stateful-1"].instance
        original_thread.stop()
        original_thread.join()
        
        # Wait for restart
        time.sleep(2.5)
        
        # Verify the new thread has the old state
        new_thread = warden._registry["stateful-1"].instance
        assert new_thread.state == important_data
        assert new_thread.state["count"] == 42
        
    finally:
        warden.shutdown()

def test_timeout_restart():
    """Test that a thread is restarted if it stops checking in."""
    warden = Thread_Warden()
    warden.start()
    
    # Very short timeout
    short_timeout = 1.0
    fast_policy = Restart_Policy(restart_delay_seconds=0.1, enable_backoff=False)
    
    try:
        warden.register_new_child("stalled-1", mock_thread_factory, policy=fast_policy, timeout=short_timeout)
        
        # Thread starts and checks in implicitly on registration (last_seen = now)
        # We just wait for timeout + monitor loop delay + restart delay
        # 1.0s timeout + 1.0s monitor loop + buffer
        
        original_thread = warden._registry["stalled-1"].instance
        
        # Wait for timeout to trigger
        time.sleep(3.0)
        
        # Warden should have killed and restarted it
        new_thread = warden._registry["stalled-1"].instance
        assert new_thread is not original_thread
        assert new_thread.is_alive()
        
        # The old thread should have been stopped
        assert original_thread.is_stopped()
        
    finally:
        warden.shutdown()

