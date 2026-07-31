# Power Monitor Requirements



## Purpose



Develop a Python-based monitoring layer that continuously reads, classifies,

and stores Jetson input-current measurements.



The first version is read-only. It detects and logs abnormal current but does

not stop CUDA, shut down Linux, or disconnect electrical power.



## Confirmed working requirements



* The expected operating-current range is 1.9 A through 2.1 A.
* Current above 2.1 A is outside the expected operating range.
* Measurements must be stored continuously while the monitor is running.
* The implementation should be written in Python.
* Current, voltage, power, timestamp, and status should be stored.
* The monitor should use the Jetson VDD\_IN sensor initially.
* The design should support later coordinator, heartbeat, CUDA, and external

power-controller integration.



## Provisional development parameters



These values are temporary until Daniel or the electrical lead confirms them.



* Normal minimum: 1.9 A
* Normal maximum: 2.1 A
* Provisional critical threshold: 2.3 A
* Provisional critical duration: 3.0 seconds
* Provisional sample interval: 0.2 seconds
* Response mode: log only
* Automatic shutdown: disabled
* Automatic power cutoff: disabled



## Proposed states



* STARTING
* LOW
* NORMAL
* OUT\_OF\_RANGE
* TRIP\_PENDING
* RED\_FLAG
* SENSOR\_ERROR
* STOPPED



## Open decisions



* Is exactly 2.1 A considered normal?
* Is exactly 2.3 A considered critical?
* How long may current remain above 2.1 A?
* Should any reading below the critical threshold reset the timer?
* Is hysteresis required?
* Should RED\_FLAG remain latched after recovery?
* What sampling rate is required?
* Is the internal VDD\_IN sensor sufficient?
* Will an external sensor also be used?
* Should a confirmed event stop CUDA?
* Should it shut down Linux?
* Should it request an external power cutoff?
* Who is authorized to clear or reset a latched event?

