from collections import namedtuple
import json

from joblib import Parallel, delayed, cpu_count
import numpy as np
import pandas as pd
from skbio.stats.ordination import PCoA
from skbio.stats.distance import DistanceMatrix

import scipy.spatial.distance as dist
from scipy.stats import entropy

def __num_dist_rows__(array, ndigits=2):
   return int(pd.DataFrame(array).sum(axis=1).map(lambda x: round(x, ndigits)).sum())

class ValidationError(ValueError):
   pass

def _input_check(topic_term_dists, doc_topic_dists, doc_lengths, vocab, term_frequency):
   ttds = topic_term_dists.shape
   dtds = doc_topic_dists.shape
   errors = []
   def err(msg):
      errors.append(msg)

   if dtds[1] != ttds[0]:
      err('Number of rows of topic_term_dists does not match number of columns of doc_topic_dists; both should be equal to the number of topics in the model.')

   if len(doc_lengths) != dtds[0]:
      err('Length of doc_lengths not equal to the number of rows in doc_topic_dists; both should be equal to the number of documents in the data.')

   W = len(vocab)
   if ttds[1] != W:
      err('Number of terms in vocabulary does not match the number of columns of topic_term_dists (where each row of topic_term_dists is a probability distribution of terms for a given topic).')
   if len(term_frequency) != W:
      err('Length of term_frequency not equal to the number of terms in the vocabulary (len of vocab).')

   if __num_dist_rows__(topic_term_dists) != ttds[0]:
      err('Not all rows (distributions) in topic_term_dists sum to 1.')

   if __num_dist_rows__(doc_topic_dists) != dtds[0]:
      err('Not all rows (distributions) in doc_topic_dists sum to 1.')

   if len(errors) > 0:
      return errors

def _input_validate(*args):
   res = _input_check(*args)
   if res:
      raise ValidationError('\n' + '\n'.join([' * ' + s for s in res]))

def jensen_shannon(_P, _Q):
    _M = 0.5 * (_P + _Q)
    return 0.5 * (entropy(_P, _M) + entropy(_Q, _M))

def js_PCoA(distributions):
   dist_matrix = DistanceMatrix(dist.squareform(dist.pdist(distributions.values, jensen_shannon)))
   pcoa = PCoA(dist_matrix).scores()
   return pcoa.site[:,0:2]

def _df_with_names(data, index_name, columns_name):
   if type(data) == pd.DataFrame:
      # we want our index to be numbered
      df = pd.DataFrame(data.values)
   else:
      df = pd.DataFrame(data)
   df.index.name = index_name
   df.columns.name = columns_name
   return df

def _series_with_name(data, name):
   if type(data) == pd.Series:
      data.name = name
      # ensures a numeric index
      return data.reset_index()[name]
   else:
      return pd.Series(data, name=name)


def _topic_coordinates(mds, topic_term_dists, topic_proportion):
   K = topic_term_dists.shape[0]
   mds_res = mds(topic_term_dists)
   assert mds_res.shape == (K, 2)
   mds_df = pd.DataFrame({'x': mds_res[:,0], 'y': mds_res[:,1], 'topics': range(1, K + 1), \
                          'cluster': 1, 'Freq': topic_proportion * 100})
   # note: cluster (should?) be deprecated soon. See: https://github.com/cpsievert/LDAvis/issues/26
   return mds_df

def _chunks(l, n):
    """ Yield successive n-sized chunks from l.
    """
    for i in range(0, len(l), n):
        yield l[i:i+n]

def _job_chunks(l, n_jobs):
   n_chunks = n_jobs
   if n_jobs < 0:
      # so, have n chunks if we are using all n cores/cpus
      n_chunks = cpu_count() + 1 - n_jobs

   return _chunks(l, n_chunks)



def _find_relevance(log_ttd, log_lift, R, lambda_):
   relevance = lambda_ * log_ttd + (1 - lambda_) * log_lift
   return relevance.T.apply(lambda s: s.order(ascending=False).index).head(R)


def _find_relevance_chunks(log_ttd, log_lift, R, lambda_seq):
   return pd.concat(map(lambda l: _find_relevance(log_ttd, log_lift, R, l), lambda_seq))


def _topic_info(topic_term_dists, topic_proportion, term_frequency, term_topic_freq, vocab, lambda_step, R, n_jobs):
   # marginal distribution over terms (width of blue bars)
   term_proportion = term_frequency / term_frequency.sum()

   # compute the distinctiveness and saliency of the terms:
   # this determines the R terms that are displayed when no topic is selected
   topic_given_term = topic_term_dists / topic_term_dists.sum()
   kernel = (topic_given_term * np.log((topic_given_term.T / topic_proportion).T))
   distinctiveness = kernel.sum()
   saliency = term_proportion * distinctiveness

   # Order the terms for the "default" view by decreasing saliency:
   default_term_info  = pd.DataFrame({'saliency': saliency, 'Term': vocab, \
                                      'Freq': term_frequency, 'Total': term_frequency, \
                                      'Category': 'Default'}). \
      sort('saliency', ascending=False). \
      head(R).drop('saliency', 1)
   ranks = np.arange(R, 0, -1)
   default_term_info['logprob'] = default_term_info['loglift'] = ranks

   ## compute relevance and top terms for each topic
   log_lift = np.log(topic_term_dists / term_proportion)
   log_ttd = np.log(topic_term_dists)
   lambda_seq = np.arange(0, 1 + lambda_step, lambda_step)

   def topic_top_term_df(tup):
      new_topic_id, (original_topic_id, topic_terms) = tup
      term_ix = topic_terms.unique()
      return pd.DataFrame({'Term': vocab[term_ix], \
                           'Freq': term_topic_freq.loc[original_topic_id, term_ix], \
                           'Total': term_frequency[term_ix], \
                           'logprob': log_ttd.loc[original_topic_id, term_ix].round(4), \
                           'loglift': log_lift.loc[original_topic_id, term_ix].round(4), \
                           'Category': 'Topic%d' % new_topic_id})

   top_terms = pd.concat(Parallel(n_jobs=n_jobs)(delayed(_find_relevance_chunks)(log_ttd, log_lift, R, ls) \
                                                 for ls in _job_chunks(lambda_seq, n_jobs)))
   topic_dfs = map(topic_top_term_df, enumerate(top_terms.T.iterrows(), 1))
   return pd.concat([default_term_info] + list(topic_dfs))

def _token_table(topic_info, term_topic_freq, vocab, term_frequency):
   # last, to compute the areas of the circles when a term is highlighted
   # we must gather all unique terms that could show up (for every combination
   # of topic and value of lambda) and compute its distribution over topics.

   # term-topic frequency table of unique terms across all topics and all values of lambda
   term_ix = topic_info.index.unique()
   term_ix.sort()
   top_topic_terms_freq = term_topic_freq[term_ix]
   # use the new ordering for the topics
   K = len(term_topic_freq)
   top_topic_terms_freq.index = range(1, K + 1)
   top_topic_terms_freq.index.name = 'Topic'

   # we filter to Freq >= 0.5 to avoid sending too much data to the browser
   token_table = pd.DataFrame({'Freq': top_topic_terms_freq.unstack()}). \
                 reset_index().set_index('term'). \
                 query('Freq >= 0.5')

   token_table['Freq'] = token_table['Freq'].round()
   token_table['Term'] = vocab[token_table.index.values].values
   # Normalize token frequencies:
   token_table['Freq'] = token_table.Freq / term_frequency[token_table.index]
   return token_table.sort(['Term', 'Topic'])

def _term_topic_freq(topic_term_dists, topic_freq, term_frequency):
   term_topic_freq = (topic_term_dists.T  * topic_freq).T
   # adjust to match term frequencies exactly (get rid of rounding error)
   err = term_frequency / term_topic_freq.sum()
   return term_topic_freq * err

def prepare(topic_term_dists, doc_topic_dists, doc_lengths, vocab, term_frequency, R=30, lambda_step = 0.01, \
            mds=js_PCoA, plot_opts={'xlab': 'PC1', 'ylab': 'PC2'}, n_jobs=-1):
   topic_term_dists = _df_with_names(topic_term_dists, 'topic', 'term')
   doc_topic_dists  = _df_with_names(doc_topic_dists, 'doc', 'topic')
   term_frequency   = _series_with_name(term_frequency, 'term_frequency')
   doc_lengths      = _series_with_name(doc_lengths, 'doc_length')
   vocab            = _series_with_name(vocab, 'vocab')
   _input_validate(topic_term_dists, doc_topic_dists, doc_lengths, vocab, term_frequency)

   topic_freq       = (doc_topic_dists.T * doc_lengths).T.sum()
   topic_proportion = (topic_freq / topic_freq.sum()).order(ascending=False)
   topic_order      = topic_proportion.index
   # reorder all data based on new ordering of topics
   topic_freq       = topic_freq[topic_order]
   topic_term_dists = topic_term_dists.ix[topic_order]
   doc_topic_dists  = doc_topic_dists[topic_order]


   # token counts for each term-topic combination (widths of red bars)
   term_topic_freq    = _term_topic_freq(topic_term_dists, topic_freq, term_frequency)
   topic_info         = _topic_info(topic_term_dists, topic_proportion, term_frequency, term_topic_freq, vocab, lambda_step, R, n_jobs)
   token_table        = _token_table(topic_info, term_topic_freq, vocab, term_frequency)
   topic_coordinates = _topic_coordinates(mds, topic_term_dists, topic_proportion)
   client_topic_order = [x + 1 for x in topic_order]

   return PreparedData(topic_coordinates, topic_info, token_table, R, lambda_step, plot_opts, client_topic_order)

class PreparedData(namedtuple('PreparedData', ['topic_coordinates', 'topic_info', 'token_table',\
                                               'R', 'lambda_step', 'plot_opts', 'topic_order'])):
    def to_dict(self):
       return {'mdsDat': self.topic_coordinates.to_dict(orient='list'),
               'tinfo': self.topic_info.to_dict(orient='list'),
               'token.table': self.token_table.to_dict(orient='list'),
               'R': self.R,
               'lambda.step': self.lambda_step,
               'plot.opts': self.plot_opts,
               'topic.order': self.topic_order}
