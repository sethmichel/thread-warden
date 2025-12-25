import threading
import queue
import Main_Globals
import config
import DataGatherer.Main_Data_Loop as Main_Data_Loop
import os
import sys
import inspect
import time
from datetime import datetime

fileName = os.path.basename(inspect.getfile(inspect.currentframe()))

'''
change log
9/22/25: completed adding watchdog thread monitor. monitors api thread, if api thread dies or stalls watchdog will restart it
and make it do the failed api call first. api thread gives watchdog key info before every api call which watchdog gives it
back on restart



how this works
-program starts starts the watchdog thread which starts the client. watchdog thread and api thread are permanently alive
-watchdog monitors if api thread timesout or if it gets super stuck long term. the api client has a 10 second timeout but
   we also need this backup handling. it basically sees if the request has been running for too long and alerts the user,
   and writes it to a separate emergency file (in case the logger breaks, there errors are hard to track so I chose to do this)
-each api function should tell the watchdog it's starting and tell it it's ending

what it's missing
-if the api timesout/stalls it could be in the middle of operations, for example) it canceled the stop loss but hasn't
   resubmitted it. so if we just call the api again it likely will fail because there's no stop loss to cancel.
   solution: decouple the api calls into many more functions, and after each call append success to a list
             in watchdog, which will use the list to call the correct functions when it tries the request again
-everything falls apart if client fails. need a client restart
-enhanced mointoring and metrics

ideas (not required):
-auto api disable during constant failures. or at least timeing changes
-give each request a priority level
-have 2 api threads, but the 1st only does get requests, the 2nd only does order submittions. this will likely reduce risk of
      a thread stalling for 15 seconds and slowing everything down, but it makes a lot of race conditions. The threads would
      need to check each other before changing any vars

open source how this works
creation/killing
- user makes a ApiWatchdog() object and calls watchdog.start_watchdog(), this will create the actual thread
- the thread uses start_watchdog() as its target function. when the user calls stop_watchdog(), it exits that functions while loop, which will kill the thread

how it tracks threads





'''

# Thread management variables (threads are started/stopped via config settings)
server_thread = None
data_gatherer_stop_event = None

# Api call management
api_queue = queue.Queue(maxsize=50)          # Queue for Api calls with max size of 50
api_fail_list = []                           # failed api calls we want to repeat
curr_api_request = None                      # current queue request we're working on right now
api_worker_thread = None                     # Single worker thread that processes the queue
api_thread_lock = threading.Lock()           # Lock for thread management
api_worker_stop_event = threading.Event()    # Event to stop the worker thread
api_queue_process_event = threading.Event()  # Event to signal queue processing
api_call_counter = 0                         # Counter for unique call IDs
api_failed_call_lookup_info = {}             # used by api_fail_list to get missing info
for ticker in config.tickers:
    # vars to load are relevant variables the function needs at that progression level
    # progression level is where it failed in the api series of calls
    api_failed_call_lookup_info[ticker] = {'vars to load': {}, 'progression level': 0}

class ApiWatchdog:
    """
    External watchdog that monitors the Api worker thread for both timeouts and indefinite hangs.
    Runs in a separate thread so it can detect when the Api worker thread is completely frozen.
    Can restart stuck threads and requeue failed requests.
    """
    def __init__(self):
        self.watchdog_thread = None                    # this is itself. the actual thread
        self.watchdog_stop_event = threading.Event()   # control the thread

        self.last_activity_time = time.time()          # checks api thread isn't stalled beyond timeout (update_activity())
        self.current_request_info = None               # the api request
        self.request_start_time = None                 # when it starts processing that api request
        self.is_processing_request = False
        self.thread_variable_holder = {}
        self.activity_lock = threading.Lock()          # protect shared data. blocks actions
        self.restart_count = 0                         # track number of restarts for stability monitoring
        self.last_restart_time = None                  # prevent rapid restart loops
        self.api_init_function = None                  # store reference to restart API
        
    # Call when starting an api request
    def start_request(self, request_info):    
        with self.activity_lock:
            self.last_activity_time = time.time()
            self.request_start_time = time.time()
            self.current_request_info = request_info
            self.is_processing_request = True

    # sometimes a thread needs to save variables and if it fails, watchdog needs to give it back those variables
    def add_thread_variables(self, var_name, var_value):    
        with self.activity_lock:
            self.thread_variable_holder[var_name] = var_value
        
    # Call periodically to show worker thread is alive
    def update_activity(self, activity_info="general_activity"):  
        with self.activity_lock:
            self.last_activity_time = time.time()
        
    # Call when Api request completes (success or failure)
    def end_request(self):
        with self.activity_lock:
            self.last_activity_time = time.time()
            self.current_request_info = None
            self.request_start_time = None
            self.is_processing_request = False
        
    # Start the watchdog monitoring thread
    def start_watchdog(self):  
        if self.watchdog_thread and self.watchdog_thread.is_alive():
            return  # Already running.
            
        def watchdog_loop():
            print("Api Watchdog started - monitoring for timeouts AND indefinite hangs")
            
            # block thread for x seconds. calling .set() will interupt this sleep
            # if .set() is used on watchdog_stop_event(), this while loop will fail and end the watchdog loop
            # in python, a thread will die when its target function ends. this function is the target function.
            # as a backup, the thread is set to None when the killing function is called, triggering the garbage collector
            while not self.watchdog_stop_event.wait(5.0):
                try:
                    current_time = time.time()
                    
                    with self.activity_lock:
                        time_since_activity = current_time - self.last_activity_time
                        is_processing = self.is_processing_request
                        request_info = self.current_request_info
                        request_start = self.request_start_time
                    
                    if is_processing and request_start:
                        request_duration = current_time - request_start
                        
                        # Detect both timeouts and indefinite hangs
                        if time_since_activity > 15.0:
                            message = (f"Api WATCHDOG: Worker thread appears STUCK/HUNG for {time_since_activity:.1f}s."
                                       f"Current request: {request_info}, Request has been running for {request_duration:.1f}s")
                            Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, message, sys.exc_info()[2].tb_lineno)
 
                            # This catches BOTH cases:
                            # - Case 1: Normal timeouts (thread is unresponsive even though you set a timeout in whatever api/process you're using. so if your timeout fails for some reason)
                            # - Case 2: Complete hangs (thread is frozen, can't call update_activity())
                            
                            self._log_emergency_hang(time_since_activity, request_duration, message)
                            
                            # Attempt to restart the stuck thread
                            if self._should_restart_thread():
                                self._restart_api_thread(request_info)
                            
                    elif time_since_activity > 30.0:  # Worker thread totally unresponsive when not processing
                        Main_Globals.logger.error(f"Api WATCHDOG: Worker thread completely unresponsive for {time_since_activity:.1f}s")
                        self._log_emergency_hang(time_since_activity, None, "No active request")
                        
                        # Attempt to restart the unresponsive thread
                        if self._should_restart_thread():
                            self._restart_api_thread(request_info)
                        
                except Exception as e:
                    # Don't let watchdog exceptions break the monitoring
                    try:
                        message = f"Api WATCHDOG: Exception in watchdog loop: {str(e)}"
                        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, message, sys.exc_info()[2].tb_lineno)
                    except:
                        pass  # Even logging failed
        
        # daemon means thread runs in background
        self.watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True, name="Watchdog_Thread")
        self.watchdog_thread.start()
        
    # kill the watchdog monitoring thread
    def stop_watchdog(self):
        if self.watchdog_thread:
            self.watchdog_stop_event.set()          # change flag to true, this flips the kill switch
            self.watchdog_thread.join(timeout=2.0)  # wait x seconds for the watchdog thread to finish dying, then continue
            self.watchdog_thread = None             # thread will die without this, this is a backup to quickly get the garbage collector on the job
            print("Api Watchdog stopped")
        

    # Store reference to API initialization function for restarts
    def set_api_init_function(self, api_init_function):
        self.api_init_function = api_init_function
        

    # Determine if thread should be restarted based on restart history and timing
    def _should_restart_thread(self):
        current_time = time.time()
        
        # Don't restart more than 3 times per hour to prevent restart loops
        if self.restart_count >= 3:
            if self.last_restart_time and (current_time - self.last_restart_time) < 3600:  # 1 hour
                Main_Globals.logger.error("Api WATCHDOG: Too many restarts in the last hour, skipping restart")
                return False
            else:
                # Reset counter after an hour
                self.restart_count = 0
        
        # Don't restart if we just restarted within the last 2 minutes
        if self.last_restart_time and (current_time - self.last_restart_time) < 120:
            Main_Globals.logger.error("Api WATCHDOG: Too soon since last restart, skipping restart")
            return False
            
        return True
        
    # Restart the stuck API thread and requeue the failed request
    def _restart_api_thread(self, failed_request_info):
        try:
            global curr_api_request, api_worker_thread, api_worker_stop_event, api_failed_call_lookup_info, api_fail_list
            
            Main_Globals.logger.error("Api WATCHDOG: Attempting to restart stuck API thread...")
            
            # Store the failed request for requeuing
            request_to_requeue = curr_api_request.copy() # shallow copy
            variables_to_load = self.thread_variable_holder
            try:
                progression_level = self.current_request_info.split(",")[0]
                if ("." in progression_level):
                    progression_level = float(progression_level)
                else:
                    progression_level = int(progression_level)
            except:
                raise ValueError(f"the info we gave to watchdog is None or not a string, fix this in the code, it should be a string. it's: {self.current_request_info}")

            api_fail_list.append(request_to_requeue)
            api_failed_call_lookup_info[request_to_requeue['ticker']]['vars to load'] = variables_to_load
            api_failed_call_lookup_info[request_to_requeue['ticker']]['progression level'] = progression_level
            
            Main_Globals.logger.info(f"Api WATCHDOG: Saving failed info for requeue: request: {request_to_requeue.get('api_target_director', 'unknown')}, "
                                     f"variables: {variables_to_load}, progression level: {progression_level}")
            
            # Log the restart attempt
            self._log_emergency_hang(0, 0, f"RESTART ATTEMPT #{self.restart_count + 1} - Failed request: {failed_request_info}, progression level: {progression_level}")
            
            # Stop the current API thread
            Stop_Api_Thread()
            time.sleep(1)  # Give thread time to clean up
            
            # Reset watchdog state
            with self.activity_lock:
                self.last_activity_time = time.time()
                self.current_request_info = None
                self.request_start_time = None
                self.is_processing_request = False
                curr_api_request = None
            
            # Start new API thread
            if self.api_init_function:
                Start_Api_Thread(self.api_init_function)
                time.sleep(2)  # Give new thread time to initialize
                
                # Requeue the failed request if it exists
                if request_to_requeue:
                    Main_Globals.logger.info(f"Api WATCHDOG: Requeuing failed request: {request_to_requeue.get('api_target_director', 'unknown')}")
                    try:
                        # Reset any failure state in the trade object
                        if 'trade' in request_to_requeue:
                            trade = request_to_requeue['trade']
                            trade.api_status = ''
                            trade.api_result = None
                        
                        # Resubmit the request
                        Submit_Api_Request(request_to_requeue)
                        
                    except Exception as requeue_error:
                        message = f"Api WATCHDOG: Failed to requeue request: {str(requeue_error)}, {str(e)}"
                        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, message, sys.exc_info()[2].tb_lineno)
                        # Set the trade to failed state if possible
                        if 'trade' in request_to_requeue:
                            trade = request_to_requeue['trade']
                            trade.api_status = 'done'
                            trade.api_result = False
                
                # Update restart tracking
                self.restart_count += 1
                self.last_restart_time = time.time()
                
                Main_Globals.logger.info(f"Api WATCHDOG: Thread restart completed successfully (restart #{self.restart_count})")
                
            else:
                Main_Globals.logger.error("Api WATCHDOG: No API init function stored, cannot restart thread")
                
        except Exception as e:
            message = f"Api WATCHDOG: Failed to restart API thread: {str(e)}"
            Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, message, sys.exc_info()[2].tb_lineno)
            self._log_emergency_hang(0, 0, f"RESTART FAILED: {str(e)}")
        
        
    # Log to emergency file when Api appears stuck
    def _log_emergency_hang(self, time_since_activity, request_duration, request_info):
        try:
            with open("Non_Code_Files/emergency_api_stuck_log.txt", "a") as f:
                f.write(f"\n{datetime.now()} - Api WORKER APPEARS STUCK\n")
                f.write(f"Time since last activity: {time_since_activity:.1f}s\n")
                if request_duration:
                    f.write(f"Current request duration: {request_duration:.1f}s\n")
                f.write(f"Request info: {request_info}\n")
                f.write(f"Thread status: {'Processing request' if self.is_processing_request else 'Idle'}\n")
                f.write(f"Restart count: {self.restart_count}\n")
                f.write("-" * 50 + "\n")
        except:
            pass  # Don't let logging failures break the watchdog


# Global watchdog instance
api_watchdog = ApiWatchdog()


# Start the data gatherer thread if not already running
def Start_Data_Gatherer_Thread():
    try:
        global server_thread, data_gatherer_stop_event
        
        if server_thread is None or not server_thread.is_alive():
            data_gatherer_stop_event = threading.Event()
            server_thread = threading.Thread(target=lambda: Main_Data_Loop.main(data_gatherer_stop_event), daemon=True, name="Data_Processing_Thread")
            server_thread.start()
            print("Data gatherer thread started")

    except Exception as e:
        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, str(e), sys.exc_info()[2].tb_lineno)


# Stop the data gatherer thread
def Stop_Data_Gatherer_Thread():
    try:
        global server_thread, data_gatherer_stop_event
        
        if data_gatherer_stop_event:
            data_gatherer_stop_event.set()
            print("Data gatherer thread stop requested")
        
        if server_thread and server_thread.is_alive():
            server_thread.join(timeout=2.0)  # Wait up to 2 seconds for thread to stop
        
        server_thread = None
        data_gatherer_stop_event = None
        
    except Exception as e:
        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, str(e), sys.exc_info()[2].tb_lineno)


# Start the Api worker thread that will handle Api requests throughout the program
def Start_Api_Thread(api_init_function):
    try:
        global api_worker_thread, api_worker_stop_event
        
        with api_thread_lock:
            # Only start if not already running
            if api_worker_thread is None or not api_worker_thread.is_alive():
                # Reset the stop event
                api_worker_stop_event.clear()
                
                # Store API init function in watchdog for restart capability
                api_watchdog.set_api_init_function(api_init_function)
                
                # Start the Api worker thread
                api_worker_thread = threading.Thread(
                    target=api_worker, 
                    args=(api_init_function,), 
                    daemon=True,
                    name="API_Worker_Thread"
                )
                api_worker_thread.start()
                print("Api worker thread started")
            else:
                print("Api worker thread is already running")

    except Exception as e:
        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, str(e), sys.exc_info()[2].tb_lineno)


# Submit an Api request to the worker thread queue and signal for processing
# WARNING: this function is called by the data thread but also by the watchdog thread if it restarts the api thread. 
#          potential race conditions (I think all impactful cases are handled by checking the thread name)
def Submit_Api_Request(api_request):
    try:
        global api_call_counter

        # Save current thread name for thread safety (prevents race conditions between watchdog and data threads)
        current_thread_name = threading.current_thread().name  # Will be "API_Watchdog", "API_Worker_Thread", or "Data_Processing_Thread"
        
        if (Main_Globals.API_SWITCH == False):
            raise ValueError(f"in api thread handler but api switch is false. it should've ever reach here. api request: {api_request}")
        
        # Check if Api thread is alive before adding to queue
        if api_worker_thread is None or not api_worker_thread.is_alive():
            raise RuntimeError(f"Api thread is not alive. Cannot add Api request to queue. Request: {api_request}")
        
        # check for request in fail queue. if there then don't submit it but do it now
        # currently it's only possible to have 1 fail request at a time, so this should be fine I think
        if (api_fail_list != [] and current_thread_name == "API_Watchdog"): # avoids race condition by checking the thread
            if (api_fail_list[0] == api_request):
                with api_thread_lock:
                    api_queue_process_event.set()  # Signal the Api thread to process the queue 
                return

        trade = api_request.get('trade')
        if trade:
            fail_timestamp = trade.api_fail_timestamps.get(api_request['api_target_director'], None)
            if (fail_timestamp != None):
                if ((datetime.now() - fail_timestamp).total_seconds() >= 5):
                    trade.api_fail_timestamp[api_request['api_target_director']] = None
                else:
                    Main_Globals.logger.info(f"{trade.ticker} - api call blocked, it failed for this call too recently")
                    if (trade.api_target_director == 'exit'):
                        trade.reason_for_exit = ''
                    trade.api_target_director = ''
                    trade.api_status = ''
                    return
            if (trade.api_target_director == 'get all orders'):
                trade.check_all_stop_loss_orders_timestamp = datetime.now().strftime("%H:%M:%S")  # must be here, and trade is the dummy trade

        Main_Globals.pending_api_requests += 1
        
        with api_thread_lock:
            api_call_counter += 1
            if 'id' not in api_request:
                api_request['id'] = f"api_call_{api_call_counter}"
            
            # Add to queue - this will block if queue is full
            api_queue.put(api_request, timeout=5.0)
            
            # Signal the Api thread to process the queue
            api_queue_process_event.set()
            
    except queue.Full:
        print(f"ERROR: Api queue is full, request {api_request.get('id', 'unknown')} dropped")
        # Set the trade to failed state if possible
        if 'trade' in api_request:
            trade = api_request['trade']
            trade.api_status = 'done'
            trade.api_result = False
            Main_Globals.pending_api_requests -= 1

    except Exception as e:
        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, str(e), sys.exc_info()[2].tb_lineno)


# Process all Api requests in the queue immediately
def Process_Api_Queue():
    try:
        # Import Api_Handler here to avoid circular import issues
        import Api_Handler
        global curr_api_request, api_queue, api_fail_list, api_failed_call_lookup_info
        
        # Function mapping for api_target_director strings
        api_function_map = {
            'enter': Api_Handler.Enter_Trade_Controller,
            'exit': Api_Handler.Exit_Trade_Controller,
            'change to holding': Api_Handler.Change_To_Holding_Controller,
            'get all orders': Api_Handler.Api_Get_All_Orders
        }
        
        # Process all items in the queue
        while not api_queue.empty():
            try:
                vars_to_load = None
                progression_level = 0
                using_failed_call = False

                if (api_fail_list != []):
                    using_failed_call = True
                    api_request = api_fail_list[0]
                    ticker = api_request['ticker']
                    vars_to_load = api_failed_call_lookup_info[ticker]['vars to load']
                    progression_level = api_failed_call_lookup_info[ticker]['progression level']
                else:
                    api_request = api_queue.get_nowait()  # Get the next Api request (non-blocking)
                    if api_request is None:  # Skip None requests
                        api_queue.task_done()
                        raise ValueError(f"thread handler sees that api request was none, request not submitted. request: {api_request}")
                               
                curr_api_request = api_request
     
                # Extract request data
                trade = api_request['trade']
                ticker = api_request['ticker']
                api_target_director = api_request['api_target_director']
                request_id = api_request.get('id', 'unknown')
                
                if (api_target_director != 'get all orders'): # happens all the time
                    print(f"Processing Api request for {ticker}: {api_target_director}")
                
                try:
                    # Get the function to call based on api_target_director
                    if api_target_director not in api_function_map:
                        raise ValueError(f"Unknown api_target_director: {api_target_director}")
                    
                    function_to_call = api_function_map[api_target_director]
                    
                    # Call the appropriate Api function
                    if api_target_director == 'enter':
                        result = function_to_call(trade, ticker, 1, trade.entry_trial_price, progression_level)
                    elif api_target_director == 'exit':
                        result = function_to_call(trade, ticker, progression_level, vars_to_load)
                    elif api_target_director == 'change to holding':
                        result = function_to_call(trade, ticker, progression_level)
                    elif (api_target_director == 'get all orders'):
                        result = function_to_call(ticker, f"isolated get all orders call (likely from data processing)", progression_level)
                        trade.all_orders = result
                    
                    # Store the result in the trade object
                    if (api_target_director != 'get all orders'):
                        trade.api_result = result
                    elif (result == False and api_target_director == 'exit'):
                        trade.reason_for_exit = ""

                    trade.api_status = 'done'
                                        
                except Exception as api_ex:
                    print(f"Error processing Api request {request_id}: {str(api_ex)}")
                    Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, str(api_ex), None)
                    
                    # Set failed result in trade object
                    trade.api_result = False
                    trade.api_status = 'done'
                
                if (using_failed_call == False):
                    api_queue.task_done()
                elif (using_failed_call == True):
                    del api_fail_list[0]
                    # Reset the failed call info instead of deleting the ticker entry
                    api_failed_call_lookup_info[ticker]['vars to load'] = {}
                    api_failed_call_lookup_info[ticker]['progression level'] = 0
                
            except queue.Empty:
                # Queue is empty, we're done
                break
                
    except Exception as e:
        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, str(e), sys.exc_info()[2].tb_lineno)


# Worker function that keeps the Api client alive and processes queue when signaled
def api_worker(api_init_function):
    try:
        # Initialize the Api client once
        print("Initializing Api client in worker thread...")
        api_init_function()
        print("\nApi client initialized successfully")
        
        # Start the external watchdog to monitor this worker thread
        api_watchdog.start_watchdog()
        
        # Keep the thread alive and process queue when signaled
        while not api_worker_stop_event.is_set():
            try:
                # Show we're alive and waiting for requests
                api_watchdog.update_activity("waiting_for_requests")
                
                # Wait for either a process signal or stop signal
                if api_queue_process_event.wait(timeout=1.0):
                    # Process signal received, clear it and process the queue
                    api_queue_process_event.clear()
                    
                    # Signal that we're starting to process the queue
                    api_watchdog.update_activity("starting_queue_processing")
                    
                    Process_Api_Queue()
                    
                    # Signal that we finished processing
                    api_watchdog.update_activity("finished_queue_processing")
                    
            except Exception as e:
                api_watchdog.update_activity(f"exception_caught: {str(e)}")
                print(f"Api worker thread interrupted: {e}")
                break
                
    except Exception as e:
        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, str(e), sys.exc_info()[2].tb_lineno)
    finally:
        api_watchdog.stop_watchdog()
        print("Api worker thread shutting down")


# Stop the Api thread gracefully
def Stop_Api_Thread():
    try:
        global api_worker_thread, api_worker_stop_event, api_queue_process_event
        
        with api_thread_lock:
            if api_worker_thread and api_worker_thread.is_alive():
                print("Stopping Api worker thread...")
                
                # Signal the worker to stop
                api_worker_stop_event.set()
                api_queue_process_event.set()  # Wake up the worker if it's waiting
                
                # Wait for thread to finish
                api_worker_thread.join(timeout=3.0)
                
                if api_worker_thread.is_alive():
                    print("Warning: Api worker thread did not stop gracefully")
                else:
                    print("Api worker thread stopped successfully")
                
                api_worker_thread = None
                
        # Clear any remaining items in the queue
        while not api_queue.empty():
            try:
                api_queue.get_nowait()
                api_queue.task_done()
            except queue.Empty:
                break
                
    except Exception as e:
        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, str(e), sys.exc_info()[2].tb_lineno)


# For backward compatibility
# Stop all Api threads gracefully (backward compatibility wrapper)
def Stop_All_Api_Threads():
    Stop_Api_Thread()


# Check MainGlobals settings and start/stop threads accordingly
def Manage_Threads():
    # Handle data gatherer thread
    if Main_Globals.DATA_GATHERER_ACTIVE:
        if server_thread is None or not server_thread.is_alive():
            Start_Data_Gatherer_Thread()
    else:
        if server_thread and server_thread.is_alive():
            Stop_Data_Gatherer_Thread()


# Stop all threads gracefully
def Stop_All_Threads(root=None):
    try:
        Stop_Data_Gatherer_Thread()
        Stop_Api_Thread()
    except Exception as e:
        Main_Globals.ErrorHandler(fileName, inspect.currentframe().f_code.co_name, str(e), sys.exc_info()[2].tb_lineno)


