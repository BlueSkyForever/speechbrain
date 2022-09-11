#!/usr/bin/env python3
import csv
import os
import re
import string
import sys
from collections import defaultdict
from pathlib import Path

import torch
import logging
import speechbrain as sb
import torchaudio
from hyperpyyaml import load_hyperpyyaml

from speechbrain.tokenizers.SentencePiece import SentencePiece
from speechbrain.utils.data_utils import undo_padding
from speechbrain.utils.distributed import run_on_main

"""Recipe for training a sequence-to-sequence ASR system with Switchboard.
The system employs a wav2vec2 encoder and a CTC decoder.
Decoding is performed with greedy decoding.

To run this recipe, do the following:
> python train_with_wav2vec2.py hparams/train_with_wav2vec2.yaml

With the default hyperparameters, the system employs a pretrained wav2vec2 encoder.
The wav2vec2 model is pretrained following the model given in the hparams file.

The neural network is trained with CTC on sub-word units (based on e.g. Byte Pairwise Encoding or a unigram language
model).

The experiment file is flexible enough to support a large variety of
different systems. By properly changing the parameter files, you can try
different encoders, decoders, tokens (e.g, characters instead of BPE), and many
other possible variations.

Authors
 * Titouan Parcollet 2021
 * Dominik Wagner 2022
"""

logger = logging.getLogger(__name__)


# Define training procedure
class ASR(sb.core.Brain):
    def __init__(
        self,
        modules=None,
        opt_class=None,
        hparams=None,
        run_opts=None,
        checkpointer=None,
        profiler=None,
    ):

        self.glm_alternatives = self._read_glm_csv(hparams["output_folder"])

        super().__init__(
            modules=modules,
            opt_class=opt_class,
            hparams=hparams,
            run_opts=run_opts,
            checkpointer=checkpointer,
            profiler=profiler,
        )

    def _read_glm_csv(self, save_folder):
        """Load the ARPA Hub4-E and Hub5-E alternate spellings and contractions map"""

        alternatives_dict = defaultdict(list)
        with open(os.path.join(save_folder, "glm.csv")) as csv_file:
            csv_reader = csv.reader(csv_file, delimiter=",")
            for row in csv_reader:
                alternatives = row[1].split("|")
                alternatives_dict[row[0]] += alternatives
        return alternatives_dict

    def compute_forward(self, batch, stage):
        """Forward computations from the waveform batches to the output probabilities."""

        batch = batch.to(self.device)
        wavs, wav_lens = batch.sig
        tokens_bos, _ = batch.tokens_bos
        wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)

        if stage == sb.Stage.TRAIN:
            if hasattr(self.hparams, "augmentation"):
                wavs = self.hparams.augmentation(wavs, wav_lens)

        # Forward pass
        feats = self.modules.wav2vec2(wavs)
        x = self.modules.enc(feats)
        logits = self.modules.ctc_lin(x)
        p_ctc = self.hparams.log_softmax(logits)

        return p_ctc, wav_lens

    def compute_objectives(self, predictions, batch, stage):
        """Computes the loss (CTC) given predictions and targets."""

        p_ctc, wav_lens = predictions

        ids = batch.id
        tokens, tokens_lens = batch.tokens

        loss = self.hparams.ctc_cost(p_ctc, tokens, wav_lens, tokens_lens)

        if stage != sb.Stage.TRAIN:
            # Decode token terms to words
            sequence = sb.decoders.ctc_greedy_decode(
                p_ctc, wav_lens, blank_id=self.hparams.blank_index
            )

            predicted_words = self.tokenizer(sequence, task="decode_from_list")

            # Convert indices to words
            target_words = undo_padding(tokens, tokens_lens)
            target_words = self.tokenizer(target_words, task="decode_from_list")

            # Check for possible word alternatives and exclusions
            if stage == sb.Stage.TEST:
                target_words, predicted_words = self.normalize_words(
                    target_words, predicted_words
                )

            self.wer_metric.append(ids, predicted_words, target_words)
            self.cer_metric.append(ids, predicted_words, target_words)

        return loss

    def expand_contractions(self, text) -> list:
        """
        Some regular expressions for expanding common contractions and for splitting linked words.

        Parameters
        ----------
        text : str
            Text to process

        Returns
        -------
        A list of tokens
        """
        # Specific contractions
        text = re.sub(r"won\'t", "WILL NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"can\'t", "CAN NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"let\'s", "LET US", text, flags=re.IGNORECASE)
        text = re.sub(r"ain\'t", "AM NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"y\'all", "YOU ALL", text, flags=re.IGNORECASE)
        text = re.sub(r"can\'t", "CANNOT", text, flags=re.IGNORECASE)
        text = re.sub(r"can not", "CANNOT", text, flags=re.IGNORECASE)
        text = re.sub(r"\'cause", "BECAUSE", text, flags=re.IGNORECASE)
        text = re.sub(r"thats", "THAT IS", text, flags=re.IGNORECASE)
        text = re.sub(r"dont", "DO NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"hes", "HE IS", text, flags=re.IGNORECASE)
        text = re.sub(r"shes", "SHE IS", text, flags=re.IGNORECASE)
        text = re.sub(r"wanna", "WANT TO", text, flags=re.IGNORECASE)
        text = re.sub(r"theyd", "THEY WOULD", text, flags=re.IGNORECASE)
        text = re.sub(r"theyre", "THEY ARE", text, flags=re.IGNORECASE)
        text = re.sub(r"hed", "HE WOULD", text, flags=re.IGNORECASE)
        text = re.sub(r"shed", "SHE WOULD", text, flags=re.IGNORECASE)
        text = re.sub(r"wouldve", "WOULD HAVE", text, flags=re.IGNORECASE)
        text = re.sub(r"couldve", "COULD HAVE", text, flags=re.IGNORECASE)
        text = re.sub(r"couldnt", "COULD NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"cant", "CAN NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"shouldve", "SHOULD HAVE", text, flags=re.IGNORECASE)
        text = re.sub(r"oclock", "O CLOCK", text, flags=re.IGNORECASE)
        text = re.sub(r"o'clock", "O CLOCK", text, flags=re.IGNORECASE)
        text = re.sub(r"didn", "DID NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"didnt", "DID NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"im", "I AM", text, flags=re.IGNORECASE)
        text = re.sub(r"ive", "I HAVE", text, flags=re.IGNORECASE)
        text = re.sub(r"youre", "YOU ARE", text, flags=re.IGNORECASE)

        # More general contractions
        text = re.sub(r"n\'t", " NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"\'re", " ARE", text, flags=re.IGNORECASE)
        text = re.sub(r"\'s", " IS", text, flags=re.IGNORECASE)
        text = re.sub(r"\'d", " WOULD", text, flags=re.IGNORECASE)
        text = re.sub(r"\'ll", " WILL", text, flags=re.IGNORECASE)
        text = re.sub(r"\'t", " NOT", text, flags=re.IGNORECASE)
        text = re.sub(r"\'ve", " HAVE", text, flags=re.IGNORECASE)
        text = re.sub(r"\'m", " AM", text, flags=re.IGNORECASE)

        # Split linked words
        if "VOCALIZED" not in text:
            text = re.sub(r"-", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s\s+", " ", text)
        text = text.split()
        return text

    def expand_contractions_batch(self, text_batch):
        """
        Wrapper that handles a batch of predicted or
        target words for contraction expansion
        """
        parsed_batch = []
        for batch in text_batch:
            # Remove incomplete words
            batch = [t for t in batch if not t.startswith("-")]
            # Expand contractions
            batch = [self.expand_contractions(t) for t in batch]
            # Flatten list of lists
            batch_expanded = [i for sublist in batch for i in sublist]
            parsed_batch.append(batch_expanded)
        return parsed_batch

    def normalize_words(self, target_words_batch, predicted_words_batch):
        """
        Remove some references and hypotheses we don't want to score.
        We remove incomplete words (i.e. words that start with "-"),
        expand common contractions (e.g. I'v -> I have),
        and split linked words (e.g. pseudo-rebel -> pseudo rebel).
        Then we check if some of the predicted words have mapping rules according
        to the glm (alternatives) file.
        Finally, we check if a predicted word is on the exclusion list.
        The exclusion list contains stuff like "MM", "HM", "AH", "HUH", which would get mapped,
        into hesitations by the glm file anyway.
        The goal is to remove all the things that appear in the reference as optional/deletable
        (i.e. inside parentheses).
        If we delete these tokens, there is no loss,
        and if we recognize them correctly, there is no gain.

        The procedure is adapted from Kaldi's local/score.sh script.

        Parameters
        ----------
        target_words_batch : list
            List of length <batch_size> containing lists of target words for each utterance
        predicted_words_batch : list of list
            List of length <batch_size> containing lists of predicted words for each utterance

        Returns
        -------

        A new list containing the filtered predicted words.

        """
        excluded_words = [
            "<UNK>",
            "UH",
            "UM",
            "EH",
            "MM",
            "HM",
            "AH",
            "HUH",
            "HA",
            "ER",
            "OOF",
            "HEE",
            "ACH",
            "EEE",
            "EW",
        ]

        target_words_batch = self.expand_contractions_batch(target_words_batch)
        predicted_words_batch = self.expand_contractions_batch(
            predicted_words_batch
        )

        # Find all possible alternatives for each word in the target utterance
        alternative2tgt_word_batch = []
        for tgt_utterance in target_words_batch:
            alternative2tgt_word = defaultdict(str)
            for tgt_wrd in tgt_utterance:
                # print("tgt_wrd", tgt_wrd)
                alts = self.glm_alternatives[tgt_wrd]
                for alt in alts:
                    if alt != tgt_wrd and len(alt) > 0:
                        alternative2tgt_word[alt] = tgt_wrd
            alternative2tgt_word_batch.append(alternative2tgt_word)

        # See if a predicted word is on the exclusion list,
        # and if it matches one of the valid alternatives.
        # Also do some cleaning.
        checked_predicted_words_batch = []
        for i, pred_utterance in enumerate(predicted_words_batch):
            alternative2tgt_word = alternative2tgt_word_batch[i]
            checked_predicted_words = []
            for pred_wrd in pred_utterance:
                # Remove stuff like [LAUGHTER]
                pred_wrd = re.sub(r"\[.*?\]", "", pred_wrd)
                # Remove any remaining punctuation
                pred_wrd = pred_wrd.translate(
                    str.maketrans("", "", string.punctuation)
                )
                # Sometimes things like LAUGHTER get appended to existing words e.g. THOUGHLAUGHTER
                if pred_wrd != "LAUGHTER" and pred_wrd.endswith("LAUGHTER"):
                    pred_wrd = pred_wrd.replace("LAUGHTER", "")
                if pred_wrd != "NOISE" and pred_wrd.endswith("NOISE"):
                    pred_wrd = pred_wrd.replace("NOISE", "")
                if pred_wrd.endswith("VOCALIZED"):
                    pred_wrd = pred_wrd.replace("VOCALIZED", "")
                # Check word exclusion list
                if pred_wrd in excluded_words:
                    continue
                # Finally, check word alternatives
                tgt_wrd = alternative2tgt_word[pred_wrd]
                if len(tgt_wrd) > 0:
                    pred_wrd = tgt_wrd
                if len(pred_wrd) > 0:
                    checked_predicted_words.append(pred_wrd)
            checked_predicted_words_batch.append(checked_predicted_words)
        return target_words_batch, checked_predicted_words_batch

    def fit_batch(self, batch):
        """Train the parameters given a single batch in input"""
        if self.auto_mix_prec:

            if not self.hparams.wav2vec2.freeze:
                self.wav2vec_optimizer.zero_grad()
            self.model_optimizer.zero_grad()

            with torch.cuda.amp.autocast():
                outputs = self.compute_forward(batch, sb.Stage.TRAIN)
                loss = self.compute_objectives(outputs, batch, sb.Stage.TRAIN)

            self.scaler.scale(loss).backward()
            if not self.hparams.wav2vec2.freeze:
                self.scaler.unscale_(self.wav2vec_optimizer)
            self.scaler.unscale_(self.model_optimizer)

            if self.check_gradients(loss):
                if not self.hparams.wav2vec2.freeze:
                    self.scaler.step(self.wav2vec_optimizer)
                self.scaler.step(self.model_optimizer)

            self.scaler.update()
        else:
            outputs = self.compute_forward(batch, sb.Stage.TRAIN)

            loss = self.compute_objectives(outputs, batch, sb.Stage.TRAIN)
            loss.backward()

            if self.check_gradients(loss):
                if not self.hparams.wav2vec2.freeze:
                    self.wav2vec_optimizer.step()
                self.model_optimizer.step()

            if not self.hparams.wav2vec2.freeze:
                self.wav2vec_optimizer.zero_grad()
            self.model_optimizer.zero_grad()

        return loss.detach()

    def evaluate_batch(self, batch, stage):
        """Computations needed for validation/test batches"""
        predictions = self.compute_forward(batch, stage=stage)
        with torch.no_grad():
            loss = self.compute_objectives(predictions, batch, stage=stage)
        return loss.detach()

    def on_stage_start(self, stage, epoch):
        """Gets called at the beginning of each epoch"""
        if stage != sb.Stage.TRAIN:
            self.cer_metric = self.hparams.cer_computer()
            self.wer_metric = self.hparams.error_rate_computer()

    def on_stage_end(self, stage, stage_loss, epoch):
        """Gets called at the end of an epoch."""
        # Compute/store important stats
        stage_stats = {"loss": stage_loss}
        if stage == sb.Stage.TRAIN:
            self.train_stats = stage_stats
        else:
            stage_stats["CER"] = self.cer_metric.summarize("error_rate")
            stage_stats["WER"] = self.wer_metric.summarize("error_rate")

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:
            old_lr_model, new_lr_model = self.hparams.lr_annealing_model(
                stage_stats["loss"]
            )
            old_lr_wav2vec, new_lr_wav2vec = self.hparams.lr_annealing_wav2vec(
                stage_stats["loss"]
            )
            sb.nnet.schedulers.update_learning_rate(
                self.model_optimizer, new_lr_model
            )
            if not self.hparams.wav2vec2.freeze:
                sb.nnet.schedulers.update_learning_rate(
                    self.wav2vec_optimizer, new_lr_wav2vec
                )
            self.hparams.train_logger.log_stats(
                stats_meta={
                    "epoch": epoch,
                    "lr_model": old_lr_model,
                    "lr_wav2vec": old_lr_wav2vec,
                },
                train_stats=self.train_stats,
                valid_stats=stage_stats,
            )
            self.checkpointer.save_and_keep_only(
                meta={"WER": stage_stats["WER"]}, min_keys=["WER"],
            )
        elif stage == sb.Stage.TEST:
            self.hparams.train_logger.log_stats(
                stats_meta={"Epoch loaded": self.hparams.epoch_counter.current},
                test_stats=stage_stats,
            )
            with open(self.hparams.wer_file, "w") as w:
                self.wer_metric.write_stats(w)

    def init_optimizers(self):
        "Initializes the wav2vec2 optimizer and model optimizer"

        # If the wav2vec encoder is unfrozen, we create the optimizer
        if not self.hparams.wav2vec2.freeze:
            self.wav2vec_optimizer = self.hparams.wav2vec_opt_class(
                self.modules.wav2vec2.parameters()
            )
            if self.checkpointer is not None:
                self.checkpointer.add_recoverable(
                    "wav2vec_opt", self.wav2vec_optimizer
                )

        self.model_optimizer = self.hparams.model_opt_class(
            self.hparams.model.parameters()
        )

        if self.checkpointer is not None:
            self.checkpointer.add_recoverable("modelopt", self.model_optimizer)


# Define custom data procedure
def dataio_prepare(hparams, tokenizer):
    """This function prepares the datasets to be used in the brain class.
    It also defines the data processing pipeline through user-defined functions."""

    # 1. Define datasets
    data_folder = hparams["data_folder"]

    train_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=hparams["train_csv"], replacements={"data_root": data_folder},
    )

    if hparams["sorting"] == "ascending":
        # we sort training data to speed up training and get better results.
        # train_data = train_data.filtered_sorted(sort_key="duration",)

        # we sort training data to speed up training and get better results.
        train_data = train_data.filtered_sorted(
            sort_key="duration",
            key_max_value={"duration": hparams["avoid_if_longer_than"]},
        )

        # when sorting do not shuffle in dataloader ! otherwise is pointless
        hparams["dataloader_options"]["shuffle"] = False

    elif hparams["sorting"] == "descending":
        # train_data = train_data.filtered_sorted(
        #     sort_key="duration", reverse=True,
        # )
        train_data = train_data.filtered_sorted(
            sort_key="duration",
            reverse=True,
            key_max_value={"duration": hparams["avoid_if_longer_than"]},
        )
        # when sorting do not shuffle in dataloader ! otherwise is pointless
        hparams["dataloader_options"]["shuffle"] = False

    elif hparams["sorting"] == "random":
        pass

    else:
        raise NotImplementedError(
            "sorting must be random, ascending or descending"
        )
    valid_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=hparams["valid_csv"], replacements={"data_root": data_folder},
    )
    # We also sort the validation data so it is faster to validate
    valid_data = valid_data.filtered_sorted(sort_key="duration")

    test_datasets = {}
    for csv_file in hparams["test_csv"]:
        name = Path(csv_file).stem
        test_datasets[name] = sb.dataio.dataset.DynamicItemDataset.from_csv(
            csv_path=csv_file, replacements={"data_root": data_folder}
        )
        test_datasets[name] = test_datasets[name].filtered_sorted(
            sort_key="duration"
        )
    datasets = [train_data, valid_data] + [i for _, i in test_datasets.items()]

    # 2. Define audio pipeline:
    @sb.utils.data_pipeline.takes("wav", "channel", "start", "stop")
    @sb.utils.data_pipeline.provides("sig")
    def audio_pipeline(wav, channel, start, stop):
        # Select a speech segment from the sph file
        # start and end times are already frames.
        # This is done in data preparation stage.
        start = int(start)
        stop = int(stop)
        num_frames = stop - start
        sig, fs = torchaudio.load(
            wav, num_frames=num_frames, frame_offset=start
        )
        info = torchaudio.info(wav)

        resampled = sig
        # Maybe resample to 16kHz
        if int(info.sample_rate) != int(hparams["sample_rate"]):
            resampled = torchaudio.transforms.Resample(
                info.sample_rate, hparams["sample_rate"],
            )(sig)

        resampled = resampled.transpose(0, 1).squeeze(1)
        # Select the proper audio channel of the segment
        if channel == "A":
            resampled = resampled[:, 0]
        else:
            resampled = resampled[:, 1]
        return resampled

    sb.dataio.dataset.add_dynamic_item(datasets, audio_pipeline)

    # 3. Define text pipeline:
    @sb.utils.data_pipeline.takes("words")
    @sb.utils.data_pipeline.provides(
        "tokens_list", "tokens_bos", "tokens_eos", "tokens"
    )
    def text_pipeline(wrd):
        tokens_list = tokenizer.sp.encode_as_ids(wrd)
        yield tokens_list
        tokens_bos = torch.LongTensor([hparams["bos_index"]] + (tokens_list))
        yield tokens_bos
        tokens_eos = torch.LongTensor(tokens_list + [hparams["eos_index"]])
        yield tokens_eos
        tokens = torch.LongTensor(tokens_list)
        yield tokens

    sb.dataio.dataset.add_dynamic_item(datasets, text_pipeline)

    # 4. Set output:
    sb.dataio.dataset.set_output_keys(
        datasets, ["id", "sig", "tokens_bos", "tokens_eos", "tokens"],
    )
    return train_data, valid_data, test_datasets


if __name__ == "__main__":

    # Load hyperparameters file with command-line overrides
    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])
    with open(hparams_file) as fin:
        hparams = load_hyperpyyaml(fin, overrides)

    # If distributed_launch=True then
    # create ddp_group with the right communication protocol
    sb.utils.distributed.ddp_init_group(run_opts)

    # Dataset preparation (parsing Switchboard)
    from switchboard_prepare import prepare_switchboard  # noqa

    # Create experiment directory
    sb.create_experiment_directory(
        experiment_directory=hparams["output_folder"],
        hyperparams_to_save=hparams_file,
        overrides=overrides,
    )

    # Due to DDP, we do the preparation ONLY on the main python process
    run_on_main(
        prepare_switchboard,
        kwargs={
            "data_folder": hparams["data_folder"],
            "save_folder": hparams["output_folder"],
            "splits": hparams["splits"],
            "split_ratio": hparams["split_ratio"],
            "skip_prep": hparams["skip_prep"],
            "add_fisher_corpus": hparams["add_fisher_corpus"],
            "max_utt": hparams["max_utt"],
        },
    )

    # Defining tokenizer and loading it
    tokenizer = SentencePiece(
        model_dir=hparams["save_folder"],
        vocab_size=hparams["output_neurons"],
        annotation_train=hparams["train_tokenizer_csv"],
        annotation_read="words",
        model_type=hparams["token_type"],
        character_coverage=hparams["character_coverage"],
    )

    # Create the datasets objects as well as tokenization and encoding
    train_data, valid_data, test_datasets = dataio_prepare(hparams, tokenizer)

    # Trainer initialization
    asr_brain = ASR(
        modules=hparams["modules"],
        hparams=hparams,
        run_opts=run_opts,
        checkpointer=hparams["checkpointer"],
    )

    # Adding objects to trainer.
    asr_brain.tokenizer = tokenizer

    # Training
    asr_brain.fit(
        asr_brain.hparams.epoch_counter,
        train_data,
        valid_data,
        train_loader_kwargs=hparams["dataloader_options"],
        valid_loader_kwargs=hparams["test_dataloader_options"],
    )

    # Test
    for k in test_datasets.keys():  # keys are test_clean, test_other etc
        asr_brain.hparams.wer_file = os.path.join(
            hparams["output_folder"], "wer_{}.txt".format(k)
        )
        asr_brain.evaluate(
            test_datasets[k],
            test_loader_kwargs=hparams["test_dataloader_options"],
        )
