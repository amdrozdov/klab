# description: scheduler + IPC stress (rt-tests hackbench), process and thread mode
set -e
groups=$(( $(nproc) * 2 ))
t=$(hackbench -P -g $groups -l 1000 -s 512 | awk '/^Time:/ {print $2}')
echo "METRIC process ${t} s lower"
t=$(hackbench -T -g $groups -l 1000 -s 512 | awk '/^Time:/ {print $2}')
echo "METRIC thread ${t} s lower"
