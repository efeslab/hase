CC 		= gcc
CFLAGS = -g -no-pie # -fno-plt
SRCS	= ./src
BINS	= ./bin
HASEBIN	= ../bin/hase
C_TARGETS = control_flow long_trace core_constraint heap_location cpu_switch repz vdso

all: $(C_TARGETS) loopy

loopy: src/loopy/loopy.S
	$(CC) -nostdlib -no-pie $(SRCS)/loopy/loopy.S -o $(BINS)/$@

$(C_TARGETS): 
	$(CC) $(CFLAGS) $(SRCS)/$@/$@.c -o $(BINS)/$@

%_record:
	sudo $(HASEBIN) record --limit=1 $(BINS)/$* 10 0
