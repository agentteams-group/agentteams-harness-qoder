AT_REF := f08cd8291c4004479660addb16fc7ecd7d08c591
AT_CONTEXT ?= https://github.com/agentscope-ai/AgentTeams.git\#$(AT_REF)
AT_SOURCE ?= ../AgentTeams
IMAGE ?= agentteams/agentteams-harness-qoder:local
CONTROLLER_IMAGE ?=
PYTHON ?= python3

.PHONY: test build
test:
	AT_SOURCE="$(AT_SOURCE)" PYTHONPATH="src:$(AT_SOURCE)/shared/lib" $(PYTHON) -m unittest discover -s tests -v

build:
	@test -n "$(CONTROLLER_IMAGE)" || { echo 'Set CONTROLLER_IMAGE to the image built from the matching Core commit' >&2; exit 1; }
	docker build --build-context at="$(AT_CONTEXT)" --build-arg CONTROLLER_IMAGE="$(CONTROLLER_IMAGE)" -t "$(IMAGE)" .
