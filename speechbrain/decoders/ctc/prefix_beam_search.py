from speechbrain.decoders.ctc.utils import CTCBaseSearcher
import torch
from itertools import groupby
from speechbrain.dataio.dataio import length_to_mask
import math
import dataclasses
import numpy as np
import heapq
import logging

logger = logging.getLogger(__name__)
import copy
from collections.abc import MutableMapping
from dataclasses import dataclass
from operator import itemgetter
from typing import Tuple

from typing import (
    Dict,
    List,
    Optional,
)


@dataclasses.dataclass
class Beam:
    """Contains all the info needed for decoding a beam."""

    text: str
    next_word: str
    partial_word: str
    last_token: Optional[str]
    last_idx_token: Optional[int]

    p: float = -math.inf
    p_b: float = -math.inf
    p_nb: float = -math.inf

    n_p_b: float = -math.inf
    n_p_nb: float = -math.inf

    score: float = -math.inf
    score_lm: float = 0.0
    score_ctc: float = -math.inf

    def step(self):
        self.p_b, self.p_nb = self.n_p_b, self.n_p_nb
        self.n_p_b = self.n_p_nb = -math.inf
        self.score_ctc = np.logaddexp(self.p_b, self.p_nb)
        self.score = self.score_ctc + self.score_lm


class Beams(MutableMapping):
    def __init__(self):
        self.beams = {(): Beam("", "", "", None, None)}
        self.beams[()].p_b = 0.0
        self.beams[()].score_ctc = 0.0

    def __getitem__(self, key):
        return self.getitem(key)

    def getitem(self, key, p=None, previous_beam=None):
        if key in self.beams:
            beam = self.beams[key]
            if p and p > beam.p:
                beam.p = p
            return beam
        
        new_beam = Beam("", "", "", None, None)
        if previous_beam:
            new_beam.p = p
        self.beams[key] = new_beam
        return new_beam

    def __setitem__(self, key, value):
        self.beams[key] = value

    def __delitem__(self, key):
        del self.beams[key]

    def __len__(self):
        return len(self.beams)

    def __iter__(self):
        return iter(self.beams)

    def step(self):

        for beam in self.beams.values():
            beam.step()

    def topk_(self, k):
        """ Keep only the top k prefixes """
        if len(self.beams) <= k:
            return self

        beams = list(self.beams.items())
        indexes = np.argpartition([-v.score for k, v in beams], k)[:k].tolist()

        self.beams = {k: v for k, v in itemgetter(*indexes)(beams)}

        return self

    def sort(self):
        return sorted(
            self.beams.items(), key=lambda x: x[1].score, reverse=True
        )


class CTCPrefixBeamSearch(CTCBaseSearcher):
    def __init__(self, blank_index, vocab_list, kenlm_model_path=None, unigrams=None, space_index=-1, beam_width=100, beam_prune_logp=-10, token_prune_min_logp=-5, history_prune=True, topk=1):
        super().__init__(blank_index, vocab_list, space_index, kenlm_model_path, unigrams, beam_width, beam_prune_logp, token_prune_min_logp, history_prune, topk)

    def partial_decoding(
        self, 
        log_probs,
        beams,
        cached_lm_scores,
        cached_p_lm_scores,
        processed_frames = 0,
    ):        
        for frame_index, logit_col in enumerate(log_probs, start=processed_frames):
            max_index = logit_col.argmax()
            tokens_index_list = set(np.where(logit_col > self.token_prune_min_logp)[0]) | {max_index}

            curr_beams = list(beams.sort())

            for token_index in tokens_index_list:
                p_token = logit_col[token_index]
                token = self.vocab_list[token_index]

                for beam in curr_beams:
                    p_b, p_nb = beam.p_b, beam.p_nb

                    # blank case
                    if token_index == self.blank_index:
                        beam.n_p_b = np.logaddexp(
                            beam.n_p_b, beam.score_ctc + p_token
                        )
                        continue

                    if token_index == beam.last_token_index:
                        beam.n_p_nb = np.logaddexp(beam.n_p_nb, p_nb + p_token)
                    
                    # la tambouille ici. 
                    exit()