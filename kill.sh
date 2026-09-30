#!/usr/bin/env bash
# ==============================================================================
# kill.sh - Clean up all AI Math Tutor background processes and free ports
# ==============================================================================

set -eo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

STOP_DOCKER=false
if [ "$1" == "--docker" ] || [ "$1" == "-d" ]; then
  STOP_DOCKER=true
fi

echo -e "${BLUE}====================================================${NC}"
echo -e "${BLUE}  AI Math Tutor - Process & Port Cleanup Script     ${NC}"
echo -e "${BLUE}====================================================${NC}\n"

# 1. Kill host processes occupying application ports
kill_port() {
  local port=$1
  local name=$2
  local pids=$(lsof -ti :"$port" 2>/dev/null || true)

  if [ -n "$pids" ]; then
    for pid in $pids; do
      local cmd=$(ps -p "$pid" -o comm= 2>/dev/null || true)
      if [[ "$cmd" =~ "docker" ]]; then
        # Check docker container publishing this port
        local container=$(docker ps --filter "publish=$port" --format "{{.Names}} ({{.ID}})" 2>/dev/null || true)
        if [ "$STOP_DOCKER" = true ]; then
          echo -e "${YELLOW}[!] Port $port is used by Docker container: $container. Stopping it...${NC}"
          local cid=$(docker ps --filter "publish=$port" --format "{{.ID}}" 2>/dev/null || true)
          if [ -n "$cid" ]; then
            docker stop "$cid" >/dev/null 2>&1 || true
            echo -e "    ${GREEN}✓ Stopped container $container${NC}"
          fi
        else
          echo -e "${YELLOW}[i] Port $port is held by Docker container: $container${NC}"
          echo -e "    ${CYAN}-> (Run with --docker to stop this container)${NC}"
        fi
      else
        echo -e "${YELLOW}[!] Found process on port $port ($name): PID $pid ($cmd)${NC}"
        echo -e "    -> Terminating PID $pid..."
        kill "$pid" 2>/dev/null || true
        sleep 0.5
        if kill -0 "$pid" 2>/dev/null; then
          echo -e "    -> Force killing (SIGKILL) PID $pid..."
          kill -9 "$pid" 2>/dev/null || true
        fi
        echo -e "    ${GREEN}✓ Terminated PID $pid.${NC}"
      fi
    done
  else
    echo -e "${GREEN}✓ Port $port ($name) is free.${NC}"
  fi
}

# 2. Terminate background processes by command line matching
kill_pattern() {
  local pattern=$1
  local label=$2
  local pids=$(pgrep -f "$pattern" 2>/dev/null || true)

  local filtered_pids=""
  for pid in $pids; do
    if [ "$pid" != "$$" ] && [ "$pid" != "$PPID" ]; then
      filtered_pids="$filtered_pids $pid"
    fi
  done

  if [ -n "$filtered_pids" ]; then
    echo -e "${YELLOW}[!] Found lingering $label: PIDs$filtered_pids${NC}"
    for pid in $filtered_pids; do
      kill "$pid" 2>/dev/null || true
      sleep 0.3
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
      echo -e "    ${GREEN}✓ Killed PID $pid ($label)${NC}"
    done
  else
    echo -e "${GREEN}✓ No lingering $label processes.${NC}"
  fi
}

echo -e "${BLUE}1. Checking and clearing application ports...${NC}"
kill_port 8000 "FastAPI / Uvicorn Server"
kill_port 3000 "Frontend Vite Dev Server"
kill_port 5173 "Vite Default Dev Server"
kill_port 7880 "LiveKit WebRTC Signal Port"
kill_port 7881 "LiveKit WebRTC TCP Port"

echo -e "\n${BLUE}2. Terminating background Python / Node workers...${NC}"
kill_pattern "uvicorn.*server:app" "Uvicorn REST Server"
kill_pattern "python.*backend/app/main.py" "LiveKit Voice Agent Worker"
kill_pattern "vite" "Vite Dev Server"

if [ "$STOP_DOCKER" = true ]; then
  echo -e "\n${BLUE}3. Stopping project Docker Compose containers...${NC}"
  if command -v docker >/dev/null 2>&1; then
    docker compose down 2>/dev/null || true
    echo -e "${GREEN}✓ Docker compose services stopped.${NC}"
  fi
else
  echo -e "\n${BLUE}3. Docker Containers:${NC}"
  echo -e "   Pass ${CYAN}./kill.sh --docker${NC} if you also want to stop Docker containers."
fi

echo -e "\n${GREEN}====================================================${NC}"
echo -e "${GREEN}  All clear! Ready to start fresh.                  ${NC}"
echo -e "${GREEN}====================================================${NC}\n"
