#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Fork 自动同步脚本
读取 repos.txt，逐个调用 GitHub merge-upstream API 把 fork 同步到上游最新。

核心策略：
- fork 与上游 identical / behind → 直接快进
- fork 与上游 diverged → 检查「领先」的提交是否全是自动产生的
  （merge commit / chore: sync 之类），是则继续合并，否则判定需人工处理
- 避免「同步越多次、ahead 越大、最终被自己判定停摆」的坑
"""
import os
import sys
import json
import time
import re
import urllib.request
import urllib.error

OWNER = 'mojinhai'
TOKEN = os.environ.get('SYNC_TOKEN', '')
API = 'https://api.github.com'
HDR = {
    'Authorization': 'token ' + TOKEN,
    'User-Agent': 'fork-sync-bot',
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
}
HERE = os.path.dirname(os.path.abspath(__file__))

# 自动产生的提交特征：命中则视为「非用户手写」，可安全继续合并
AUTO_PATTERNS = [
    re.compile(r'^chore:\s*sync from upstream', re.I),   # 本脚本/本地脚本产生
    re.compile(r'^Merge (branch|remote-tracking branch|pull request)', re.I),
    re.compile(r"^Merge branch '.*' into .*", re.I),
    re.compile(r'^Merge .+ into .+', re.I),
]


def api(method, path, body=None):
    """调用 GitHub API，返回 (状态码, 解析后的 JSON 或文本)"""
    data = json.dumps(body).encode() if body is not None else None
    h = dict(HDR)
    if data:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def load_manifest():
    """解析清单，返回 (同步列表, 观察列表)"""
    do, watch = [], []
    path = os.path.join(HERE, 'repos.txt')
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('|')
            if parts[0] == '!watch':
                if len(parts) >= 3:
                    watch.append((parts[1], parts[2]))
            elif len(parts) >= 2:
                do.append((parts[0], parts[1]))
    return do, watch


def is_auto_commit(msg):
    """判断提交信息是否属于自动产生（非用户手写）"""
    first = msg.split('\n')[0].strip()
    return any(p.search(first) for p in AUTO_PATTERNS)


def compare(upstream, up_branch, fork_branch):
    """返回 compare 结果 dict，失败返回 None"""
    st, d = api('GET', '/repos/%s/compare/%s...%s:%s'
                % (upstream, up_branch, OWNER, fork_branch))
    if st == 200 and isinstance(d, dict):
        return d
    return None


def sync_one(fork, upstream):
    """同步单个 fork，返回 (状态, 说明)"""
    # 1. 上游默认分支
    st, u = api('GET', '/repos/' + upstream)
    if st != 200 or not isinstance(u, dict):
        return 'skip', '上游仓库不存在或无法访问'
    up_branch = u.get('default_branch')

    # 2. fork 默认分支
    st, f = api('GET', '/repos/%s/%s' % (OWNER, fork))
    if st != 200 or not isinstance(f, dict):
        return 'skip', 'fork 不存在或无法访问'
    fork_branch = f.get('default_branch')
    if not fork_branch:
        return 'skip', 'fork 无默认分支'

    # 3. 比较差异
    c = compare(upstream, up_branch, fork_branch)
    if c is None:
        return 'skip', '无法比较（可能无共同祖先）'
    behind = c.get('behind_by', 0)
    ahead = c.get('ahead_by', 0)
    status = c.get('status')

    if status == 'identical' or (behind == 0 and ahead == 0):
        return 'ok', '已是最新'
    if behind == 0:
        return 'ok', '已是最新（本地领先 %d 个提交）' % ahead

    # 4. diverged：检查领先提交是否全为自动产生
    if status == 'diverged' and ahead > 0:
        commits = c.get('commits') or []
        # compare 的 commits 是「上游有而 fork 没有」的，需另取 fork 独有提交
        st2, fc = api('GET', '/repos/%s/compare/%s:%s...%s:%s'
                      % (OWNER, fork_branch, upstream, up_branch))
        # 换一种方式：直接列出 fork 分支最近提交，找出不在上游的
        st3, recent = api('GET', '/repos/%s/commits?sha=%s&per_page=30'
                          % (fork, fork_branch))
        manual = []
        if st3 == 200 and isinstance(recent, list):
            for cm in recent:
                sha = cm.get('sha')
                # 该提交是否存在于上游
                st4, _ = api('GET', '/repos/%s/commits/%s' % (upstream, sha))
                if st4 == 200:
                    continue  # 上游已有，跳过
                msg = cm.get('commit', {}).get('message', '')
                if not is_auto_commit(msg):
                    manual.append((sha[:8], msg.split('\n')[0][:50]))

        if manual:
            return 'watch', ('分叉：fork 上有 %d 个手写提交（如 %s），'
                             '落后 %d 需人工处理'
                             % (len(manual), manual[0][1], behind))
        # 全是自动提交 → 继续合并

    # 5. 执行 merge-upstream
    st, d = api('POST', '/repos/%s/%s/merge-upstream' % (OWNER, fork),
                {'branch': fork_branch})
    if st == 200 and isinstance(d, dict):
        mt = d.get('merge_type', '?')
        if mt == 'none':
            return 'ok', '已是最新'
        return 'ok', '同步成功（%s，落后 %d → 0）' % (mt, behind)
    if st == 409:
        return 'watch', '存在冲突，需人工解决（落后 %d / 领先 %d）' % (behind, ahead)
    if st == 422:
        msg = d.get('message', '') if isinstance(d, dict) else str(d)
        return 'watch', '被拒绝：%s' % str(msg)[:100]
    msg = d.get('message', '') if isinstance(d, dict) else str(d)
    return 'fail', 'HTTP %s：%s' % (st, str(msg)[:100])


def main():
    if not TOKEN:
        print('❌ 缺少 SYNC_TOKEN 环境变量')
        sys.exit(1)

    do_list, watch_list = load_manifest()
    results = {'ok': [], 'skip': [], 'watch': [], 'fail': []}

    print('=' * 62)
    print('GitHub Fork 自动同步  |  共 %d 个待同步 + %d 个观察' %
          (len(do_list), len(watch_list)))
    print('=' * 62)

    for fork, upstream in do_list:
        print('\n▶ %s  ←  %s' % (fork, upstream))
        try:
            status, msg = sync_one(fork, upstream)
        except Exception as e:
            status, msg = 'fail', '异常：%s' % str(e)[:100]
        icon = {'ok': '✅', 'skip': '⏭️', 'watch': '👀', 'fail': '❌'}[status]
        print('  %s %s' % (icon, msg))
        results[status].append((fork, msg))
        time.sleep(1)

    # 观察区只报告
    if watch_list:
        print('\n' + '-' * 62)
        print('👀 观察区（不做任何改动）')
        print('-' * 62)
        for fork, upstream in watch_list:
            st, u = api('GET', '/repos/' + upstream)
            if st != 200 or not isinstance(u, dict):
                print('  ⚠️ %-24s 上游不存在' % fork)
                continue
            st, f = api('GET', '/repos/%s/%s' % (OWNER, fork))
            if st != 200 or not isinstance(f, dict):
                print('  ⚠️ %-24s fork 无法访问' % fork)
                continue
            c = compare(upstream, u.get('default_branch'), f.get('default_branch'))
            if c is None:
                print('  ⚠️ %-24s 无法比较（无共同祖先）' % fork)
            else:
                print('  👀 %-24s 落后 %-6s 领先 %-6s %s'
                      % (fork, c.get('behind_by'), c.get('ahead_by'), c.get('status')))
            time.sleep(0.8)

    print('\n' + '=' * 62)
    print('📊 汇总：成功 %d | 跳过 %d | 需人工 %d | 失败 %d'
          % (len(results['ok']), len(results['skip']),
             len(results['watch']), len(results['fail'])))
    print('=' * 62)

    if results['fail']:
        sys.exit(1)


if __name__ == '__main__':
    main()