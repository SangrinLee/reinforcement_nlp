# -*- coding: utf-8 -*-
import argparse
import torch
import torch.cuda as cuda
from torch.autograd import Variable
from word_lstm_model import *
import time
import math
import string
import pickle
import numpy as np
import bisect
import word_corpus_data as data
import re
import operator
import chainer

#################################################
# Hyper-parameters
#################################################
parser = argparse.ArgumentParser(description='PyTorch PennTreeBank Character-Level LSTM Model')
parser.add_argument('--nhid', type=int, default=128,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=2,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=3,
                    help='initial learning rate')
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--load_epochs', type=int, default=0,
                    help='load epoch')
parser.add_argument('--epochs', type=int, default=15,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=5, metavar='N',
                    help='batch size')
parser.add_argument('--bptt', type=int, default=100,
                    help='sequence length')
parser.add_argument('--dropout', type=float, default=0.25,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--cuda', action='store_true', default=True,
                    help='use CUDA')
parser.add_argument('--bidirectional', action='store_true', default=False,
                    help='use Bi-LSTM')
parser.add_argument('--serialize', action='store_true', default=False, #False,
                    help='continue training a stored model')
parser.add_argument('--log-interval', type=int, default=20, metavar='N',
                    help='report interval')
args = parser.parse_args()

#############################################
# Load data
#############################################
# Extract data
train, val, test = chainer.datasets.get_ptb_words()
# Dictionary between id and word
ptb_dict = chainer.datasets.get_ptb_words_vocabulary()
# Number of words
words_num = len(ptb_dict)

# Reverse dictionary
ptb_word_id_dict = ptb_dict
ptb_id_word_dict = dict((v,k) for k,v in ptb_word_id_dict.items())

# id for <eos>
eos_idx = ptb_dict['<eos>']

# Indices in train data corresponding to <eos>
eos_list = np.where(train==eos_idx)[0]

# Extract sentences
sentence_list = []

eos_idx_prev = 0
for eos_idx_curr in eos_list:
    sentence = train[eos_idx_prev:eos_idx_curr]
    sentence_list.append(sentence)
    eos_idx_prev = eos_idx_curr+1
sentence_num = len(sentence_list) # Number of sentences
    
# Frequency table of words
freq_list = np.zeros(words_num)
for i in range(words_num):
    freq_list[i] = np.sum(train==i)
    
prob_words = freq_list/len(train)

# Sort
idx_list_sorted = np.argsort(freq_list) # Low to high
freq_list_sorted = freq_list[idx_list_sorted]

# Remove top 50%
idx_list_rare = idx_list_sorted[0:words_num//2]   # Words corresponding to bottom 50% frequency
freq_list_rare = freq_list_sorted[0:words_num//2]

# Go through every sentence, every word
p_list = []                 # Rare word fraction (inverse weights)
sentence_kept_list = []     # Sentences kept for labelling

for i in range(sentence_num):
    sentence = sentence_list[i]
    num_total = len(sentence)   # Total number of words
    num_rare = 0                # Number of rare words
    for word in sentence:
        if word in idx_list_rare:   # If word is rare
            num_rare += 1
            
    p_i = (num_rare+1)/num_total    # Prob. of sampling
    
    # Cutoff
    if p_i > 1:
        p_i = 1
        
    r = np.random.random()
    if r < p_i:
        p_list.append(p_i)  
        sentence_kept_list.append(sentence)
        
train_less = np.concatenate(sentence_kept_list)
n_letters = len(ptb_word_id_dict)
n_categories = len(ptb_word_id_dict)
train_data_array = train_less
val_data_array = train_less

path = 'word_data'

########################################################
# Pre-process training and validation data
########################################################
def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data[0: nbatch * bsz]
    # Evenly divide the data across the bsz batches.
    data = data.reshape(bsz, -1)
    return data

# Wraps hidden states in new Variables, to detach them from their history.
def repackage_hidden(h):
    if type(h) == Variable:
        return Variable(h.data)
    else:
        return tuple(repackage_hidden(v) for v in h)

# for every batch you call this to get the batch of data and targets, in testing your only getting astericks for the characters you took out, 
# you have the index but the data is turned into 3D array, you have to map indices that you have, 
# you have to search for what astericks/how many are in the array, and then you go back using the indices of the astericks you already know
# to map that into indices of output array that you have 
def get_batch(source, i, evaluation=False):
    seq_len = min(args.bptt, source.shape[1] - 1 - i) # -1 so that there's data for the target of the last time step
    data = source[:, i: i + seq_len] 
    source_target = source.astype(np.int64)
    target = source_target[:, i + 1: i + 1 + seq_len]

    # initialize train_data_tensor, test_data_tensor
    data_embedding = np.zeros((data.shape[0], data.shape[1], n_letters), dtype = np.float32)

    # convert 2D numpy array to 3D numpy embedding
    for i in range(0, data.shape[0]):
        for j in range(0, data.shape[1]):
            data_embedding[i][j][data[i][j]] = 1

    # create tensor variable
    data_embedding = torch.from_numpy(data_embedding)
    data_embedding = Variable(data_embedding, volatile=evaluation)    # Saves memory in purely inference mode

    target = torch.from_numpy(target)
    target = Variable(target, volatile=evaluation)
    if args.bidirectional:
        # r_target of length seq_len - 1
        r_source_target = np.flip(source_target[:, i - 1: i - 1 + seq_len].cpu().numpy(), 1).copy()
        target = torch.cat((Variable(source_target[:, i + 1: i + 1 + seq_len].contiguous().view(-1)),
                            Variable(torch.from_numpy(r_source_target).cuda().contiguous().view(-1))), 0)
    else:
        target = target.contiguous().view(-1)
    if args.cuda:
        return data_embedding.cuda(), target.cuda()
    else:
        return data_embedding, target

def embed(data_array, bsz):
    # convert 1D array to 2D
    data_array = batchify(data_array, bsz)

    # initialize train_data_tensor, test_data_tensor
    data_embedding = np.zeros((data_array.shape[0], data_array.shape[1], n_letters), dtype = np.float32)

    # convert 2D numpy array to np.int64
    data_array = data_array.astype(np.int64)

    # convert 2D numpy array to 3D numpy embedding
    for i in range(0, data_array.shape[0]):
        for j in range(0, data_array.shape[1]):
            data_embedding[i][j][data_array[i][j]] = 1

    # convert 2D numpy array to 2D target tensor
    return data_embedding, data_array

def find_ge(a, x):
    i = bisect.bisect_left(a, x)
    return i

def find_le(a, x):
    i = bisect.bisect_right(a, x)
    return i - 1

###############################################################################
# Build the model
###############################################################################
if args.bidirectional:
    name = 'Bi-LSTM'
else:
    name = 'LSTM'

if args.serialize:
    with open(path + '/{}_Epoch{}_BatchSize{}_Dropout{}_LR{}_HiddenDim{}.pt'.format(
             name, args.load_epochs, args.batch_size, args.dropout, args.lr, args.nhid), 'rb') as f:
        model = torch.load(f)
else:
    model = MyLSTM(n_letters, args.nhid, args.nlayers, True, True, args.dropout, args.bidirectional, args.batch_size, args.cuda)
    args.load_epochs = 0

if args.cuda:
    model.cuda()

criterion = nn.CrossEntropyLoss()
softmax = nn.Softmax()
NLL = nn.NLLLoss()

if args.cuda:
    criterion.cuda()
    softmax.cuda()
    NLL.cuda()

val_bsz = 5
train_data = batchify(train_data_array, args.batch_size)
val_data = batchify(val_data_array, val_bsz)

def train():
    # Turn on training mode which enables dropout.
    # Built-in function, has effect on dropout and batchnorm
    model.train()
    total_loss = 0
    start_time = time.time()
    hidden = model.init_hidden(args.batch_size)

    batch_length = train_data_array.size // args.batch_size
    for batch, i in enumerate(range(1, train_data.shape[1] - 1, args.bptt)):
        # returns Variables
        data, targets = get_batch(train_data, i)
        
        if not args.bidirectional:
            hidden = model.init_hidden(args.batch_size)
        else:
            hidden = repackage_hidden(hidden)
        model.zero_grad()
        output, hidden = model(data, hidden)

        loss = criterion(output, targets)
        loss.backward()

        torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)
        for p in model.parameters():
            p.data.add_(-lr, p.grad.data)   # (scalar multiplier, other tensor)

        total_loss += loss.data
        if batch % args.log_interval == 0 and batch > 0:
            cur_loss = total_loss[0] / args.log_interval
            elapsed = time.time() - start_time
            print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                  'loss {:5.2f} | ppl {:5.2f} |'.format(
                   epoch, batch, train_data.shape[1] // args.bptt, lr,
                   elapsed * 1000 / args.log_interval, cur_loss, math.exp(cur_loss)))
            total_loss = 0
            start_time = time.time()

# Uses training data to generate predictions, calculate loss based on validation/testing data
# Not using bptt
def evaluate():
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0
    hidden = model.init_hidden(val_bsz)
    start_time = time.time()

    batch_length = val_data_array.size // val_bsz
    for batch, i in enumerate(range(1, val_data.shape[1] - 1, args.bptt)):
        # returns Variables
        data, targets = get_batch(val_data, i)

        if not args.bidirectional:
            hidden = model.init_hidden(val_bsz)
        else:
            hidden = repackage_hidden(hidden)

        output, hidden = model(data, hidden)
        loss = criterion(output, targets)
        total_loss += loss.data

        if batch % (args.log_interval // 20) == 0 and batch > 0:
            elapsed = time.time() - start_time
            print('| validation | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                  .format(batch, val_data.shape[1] // args.bptt, lr,
                          elapsed * 1000 / (args.log_interval // 20)))
            start_time = time.time()
    return total_loss[0] / (batch_length / args.bptt)

# Loop over epochs.
lr = args.lr
best_val_loss = None

# Training Part
# At any point you can hit Ctrl + C to break out of training early.
arr1 = []
try:
    for epoch in range(args.load_epochs+1, args.epochs+args.load_epochs+1):
        epoch_start_time = time.time()
        train()
        
        val_loss = evaluate()
        print('-' * 89)
        print('| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} |'.format(
            epoch, (time.time() - epoch_start_time),
            val_loss))
        print('-' * 89)

        # Save all models from each epoch
        with open(path + '/{}_Epoch{}_BatchSize{}_Dropout{}_LR{}_HiddenDim{}.pt'.format(
           name, epoch, args.batch_size, args.dropout, args.lr, args.nhid), 'wb') as f:
            torch.save(model, f)

        # check if the validation loss is the best we've seen so far.
        if not best_val_loss or val_loss < best_val_loss:
            best_val_loss = val_loss
        else:
            # Anneal the learning rate if no improvement has been seen in the validation dataset.
            lr /= 4.0
        
except KeyboardInterrupt:
   print('-' * 89)
   print('Exiting from training early')

#Load the best saved model.
with open(path + '/{}_Epoch{}_BatchSize{}_Dropout{}_LR{}_HiddenDim{}.pt'.format(
               name, args.load_epochs+args.epochs, args.batch_size, args.dropout, args.lr, args.nhid), 'rb') as f:
    model = torch.load(f)
