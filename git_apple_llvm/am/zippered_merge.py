"""
  Module for performing a zippered merge between two branches.

  A zippered merge is used to merge branches based on a matching
  merge-base commit from the common ancestor. For example,
  given the following commit graph:

  ancestor -> left   - > rejoin
           -> right  /

  the zippered merge would merge commits from left and right into
  the rejoin edge, as long as their ancestor merge base allows them
  to be rejoined. In particular, the following scenarios are allowed:

  - A commit from left can be merged directly when
    the commit's merge base is the same as the existing merge base of right.

  - A commit from right can be merged directly when
    the commit's merge base is the same as the existing merge base of left.

  - A commit from left and right should be merged together when
    both of them have a matching merge base.

  The algorithm favours the left for both direct merges (left's direct
  merges are performed before the right), and when looking for the next
  matching merge base between the left and right branch.
"""

from typing import List, Optional
from git_apple_llvm.am.core import compute_unmerged_commits
from git_apple_llvm.git_tools import git_output
import logging


log = logging.getLogger(__name__)


class BranchIterator:
    def __init__(self, commits: List[str], merge_base_generator, initial_merge_base: str):
        self.commits: List[str] = commits
        self.merge_base_generator = merge_base_generator
        self.cached_merge_bases: List[Optional[str]] = list([None for x in commits])
        assert len(self.commits) == len(self.cached_merge_bases)
        self.i: int = 0
        self.len: int = len(self.commits)
        self.initial_merge_base: str = initial_merge_base

    @property
    def has_commits(self) -> bool:
        return self.i < self.len

    def _get_merge_base(self, idx) -> str:
        if self.cached_merge_bases[idx] is not None:
            return self.cached_merge_bases[idx]
        result: str = self.merge_base_generator(self.commits[idx])
        self.cached_merge_bases[idx] = result
        return result

    @property
    def prev_merge_base(self) -> str:
        return self.initial_merge_base if self.i == 0 else self._get_merge_base(self.i - 1)

    @property
    def current_merge_base(self) -> str:
        return self._get_merge_base(self.i)

    def takeCommit(self) -> str:
        result = self.commits[self.i]
        self.i += 1
        return result

    def find_matching_merge_base(self, right) -> Optional[int]:
        i = self.i
        while i < self.len:
            if self._get_merge_base(i) == right.current_merge_base:
                return i
            i += 1
        return None


def find_next_matching_merge_base(left: BranchIterator, right: BranchIterator) -> bool:
    while left.has_commits and right.has_commits:
        # Try looking through the left side, until we find a matching right
        # merge base.
        left_match: Optional[int] = left.find_matching_merge_base(right)
        if left_match is not None:
            left.i = left_match
            return True
        # No commit merge base found, advance the right.
        right.takeCommit()
    return False


def compute_zippered_merge_commits(left: BranchIterator, right: BranchIterator,
                                   stop_on_first: bool = False) -> List[List[str]]:
    """
       Return the list of lists that contain the parents of the merge commit that should
       be constructed to perform the required zippered merges.
    """
    merges: List[List[str]] = []

    while left.has_commits or right.has_commits:
        if stop_on_first and len(merges) > 0:
            break
        # Try merging commits from one branch first, while the merge base allows it.
        if left.has_commits and right.prev_merge_base == left.current_merge_base:
            merges.append([left.takeCommit()])
            continue
        if right.has_commits and left.prev_merge_base == right.current_merge_base:
            merges.append([right.takeCommit()])
            continue

        if find_next_matching_merge_base(left, right):
            # If both merge bases match, construct a merge from both branches in one commit.
            assert left.current_merge_base == right.current_merge_base
            merges.append([left.takeCommit(), right.takeCommit()])
            continue
        break

    return merges


def compute_zippered_merges(remote: str, target: str, left_upstream: str,
                            right_upstream: str, common_ancestor: str,
                            max_commits: int = 140,
                            stop_on_first: bool = False) -> Optional[List[List[str]]]:
    """
        Return the list of lists that contain the parents of the merge commit that should
        be constructed to perform merges into the zippered branch, or None if the zippered
        branch is up-to-date.
    """
    def emptyIfNoneOrReversed(x: Optional[List[str]]) -> List[str]:
        return [] if not x else list(reversed(x))
    # Compute the unmerged commit lists.
    left_commits: List[str] = emptyIfNoneOrReversed(compute_unmerged_commits(remote=remote, target_branch=target,
                                                    upstream_branch=left_upstream))
    right_commits: List[str] = emptyIfNoneOrReversed(compute_unmerged_commits(remote=remote, target_branch=target,
                                                     upstream_branch=right_upstream))
    if len(left_commits) == 0 and len(right_commits) == 0:
        # The branches are up-to-date.
        return None
    # Restrict the size of the merge window, as computing a lot of merge bases
    # is expensive.
    if len(left_commits) > max_commits:
        left_commits = left_commits[:max_commits]
    if len(right_commits) > max_commits:
        right_commits = right_commits[:max_commits]

    def computeMergeBase(commit: str) -> str:
        return git_output('merge-base', commit, f'{remote}/{common_ancestor}')

    def computeInitialMergeBase(branch: str, commits: List[str]) -> str:
        # FIXME: This might be wrong if someone did a reverse merge into the upstream branch,
        # and the first parent of the first commit doesn't reach the common ancestor.
        return computeMergeBase(f'{remote}/{branch}' if len(commits) == 0 else f'{commits[0]}^')

    log.debug(f'setting up left branch iterator for zippered merge to {target}')
    left = BranchIterator(left_commits, computeMergeBase,
                          computeInitialMergeBase(left_upstream, left_commits))
    log.debug(f'setting up right branch iterator for zippered merge to {target}')
    right = BranchIterator(right_commits, computeMergeBase,
                           computeInitialMergeBase(right_upstream, right_commits))
    merges: List[List[str]] = compute_zippered_merge_commits(left, right, stop_on_first)
    log.debug(f'zippered merge algorithm produced {len(merges)} merges')
    return merges
