# Copyright 2020 The TensorFlow Recommenders Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Functions supporting Movielens examples."""

import array
import collections
import random

from typing import Dict, List, Optional, Set, Text, Tuple

import numpy as np
import tensorflow as tf


def evaluate(user_model: tf.keras.Model,
             movie_model: tf.keras.Model,
             test: tf.data.Dataset,
             movies: tf.data.Dataset,
             train: Optional[tf.data.Dataset] = None,
             k: int = 10) -> Dict[Text, float]:
  """Evaluates a Movielens model on the supplied datasets.

  Args:
    user_model: User representation model.
    movie_model: Movie representation model.
    test: Test dataset.
    movies: Dataset of movies.
    train: Training dataset. If supplied, recommendations for training watches
      will be removed.
    k: The cutoff value at which to compute precision and recall.

  Returns:
   Dictionary of metrics.
  """

  movie_ids = np.concatenate(
      list(movies.batch(1000).map(lambda x: x["movie_id"]).as_numpy_iterator()))

  movie_vocabulary = dict(zip(movie_ids.tolist(), range(len(movie_ids))))

  train_user_to_movies = collections.defaultdict(lambda: array.array("i"))
  test_user_to_movies = collections.defaultdict(lambda: array.array("i"))

  if train is not None:
    for row in train.as_numpy_iterator():
      user_id = row["user_id"]
      movie_id = movie_vocabulary[row["movie_id"]]
      train_user_to_movies[user_id].append(movie_id)

  for row in test.as_numpy_iterator():
    user_id = row["user_id"]
    movie_id = movie_vocabulary[row["movie_id"]]
    test_user_to_movies[user_id].append(movie_id)

  movie_embeddings = np.concatenate(
      list(movies.batch(4096).map(
          lambda x: movie_model({"movie_id": x["movie_id"]})
      ).as_numpy_iterator()))

  precision_values = []
  recall_values = []

  for (user_id, test_movies) in test_user_to_movies.items():
    user_embedding = user_model({"user_id": np.array([user_id])}).numpy()
    scores = (user_embedding @ movie_embeddings.T).flatten()

    test_movies = np.frombuffer(test_movies, dtype=np.int32)

    if train is not None:
      train_movies = np.frombuffer(
          train_user_to_movies[user_id], dtype=np.int32)
      scores[train_movies] = -1e6

    top_movies = np.argsort(-scores)[:k]
    num_test_movies_in_k = sum(x in top_movies for x in test_movies)
    precision_values.append(num_test_movies_in_k / k)
    recall_values.append(num_test_movies_in_k / len(test_movies))

  return {
      "precision_at_k": np.mean(precision_values),
      "recall_at_k": np.mean(recall_values)
  }


def _sample_list(
    negative_movie_id_set: Set,
    feature_lists: Dict[Text, List[tf.Tensor]],
    num_pos_examples_per_list: int,
    num_neg_examples_per_list: int) -> Tuple[tf.Tensor]:
  """Function for sampling a list example from given feature lists."""
  sampled_indices = random.sample(
      range(len(feature_lists["movie_id"])),
      num_pos_examples_per_list,
  )
  sampled_pos_movie_ids = [
      feature_lists["movie_id"][idx]
      for idx in sampled_indices
  ]
  sampled_pos_ratings = [
      feature_lists["user_rating"][idx]
      for idx in sampled_indices
  ]
  sampled_neg_movie_ids = random.sample(
      negative_movie_id_set,
      num_neg_examples_per_list,
  )
  # Assign score 0 to movies that are not rated by the user, so they would
  # be placed at the bottom of the ranking.
  sampled_neg_ratings = [
      0. for _ in range(num_neg_examples_per_list)
  ]
  sampled_movie_ids = sampled_pos_movie_ids + sampled_neg_movie_ids
  sampled_ratings = sampled_pos_ratings + sampled_neg_ratings
  return (
      tf.concat(sampled_movie_ids, 0),
      tf.concat(sampled_ratings, 0),
  )


def movielens_to_listwise(
    rating_dataset: tf.data.Dataset,
    movie_dataset: tf.data.Dataset,
    train_num_list_per_user: int = 10,
    test_num_list_per_user: int = 2,
    num_pos_examples_per_list: int = 10,
    num_neg_examples_per_list: int = 0) -> Tuple[tf.data.Dataset]:
  """Function for converting the MovieLens 100K dataset to a listwise dataset.

  Args:
      rating_dataset:
        The MovieLens 100K ratings dataset loaded from TFDS.
      movie_dataset:
        The MovieLens 100K movies dataset loaded from TFDS.
      train_num_list_per_user:
        An integer representing the number of lists that should be sampled for
        each user in the training dataset.
      test_num_list_per_user:
        An integer representing the number of lists that should be sampled for
        each user in the testing dataset.
      num_pos_examples_per_list:
        An integer representing the number of movies to be sampled for each list
        from the list of movies rated by the user.
      num_neg_examples_per_list:
        An integer representing the number of movies in each list to be sampled
        from movies that are not rated by the user.

  Returns:
      A tuple of tensorflow datasets, each containing list examples. The first
      dataset is the training dataset and the second dataset is the testing
      dataset.

      Each example contains three keys: "user_id", "movie_id", and
      "user_rating". "user_id" maps to a string tensor that represents the user
      id for the example. "movie_id" maps to a tensor of shape
      [sum(num_example_per_list)] with dtype tf.string. It represents the list
      of candidate movie ids. "user_rating" maps to a tensor of shape
      [sum(num_example_per_list)] with dtype tf.float32. It represents the
      rating of each movie in the candidate list. Movies that were not rated by
      the user in an example would receive a rating of 0.
  """
  example_lists_by_user = collections.defaultdict(lambda: {
      "movie_id": [],
      "user_rating": [],
  })
  for example in rating_dataset:
    user_id = example["user_id"].numpy()
    example_lists_by_user[user_id]["movie_id"].append(
        example["movie_id"],
    )
    example_lists_by_user[user_id]["user_rating"].append(
        example["user_rating"],
    )
  movie_id_vocab = set(
      movie_dataset.map(lambda x: x["movie_id"]).as_numpy_iterator(),
  )

  train_tensor_slices = {"user_id": [], "movie_id": [], "user_rating": []}
  test_tensor_slices = {"user_id": [], "movie_id": [], "user_rating": []}
  for user_id, feature_lists in example_lists_by_user.items():
    rated_movie_id_set = set([
        example.numpy()
        for example in feature_lists["movie_id"]
    ])
    negative_movie_id_set = movie_id_vocab - rated_movie_id_set
    for _ in range(train_num_list_per_user):
      sampled_movie_ids, sampled_ratings = _sample_list(
          negative_movie_id_set,
          feature_lists,
          num_pos_examples_per_list,
          num_neg_examples_per_list,
      )
      train_tensor_slices["user_id"].append(user_id)
      train_tensor_slices["movie_id"].append(sampled_movie_ids)
      train_tensor_slices["user_rating"].append(sampled_ratings)
    for _ in range(test_num_list_per_user):
      sampled_movie_ids, sampled_ratings = _sample_list(
          negative_movie_id_set,
          feature_lists,
          num_pos_examples_per_list,
          num_neg_examples_per_list,
      )
      test_tensor_slices["user_id"].append(user_id)
      test_tensor_slices["movie_id"].append(sampled_movie_ids)
      test_tensor_slices["user_rating"].append(sampled_ratings)
  return (
      tf.data.Dataset.from_tensor_slices(train_tensor_slices),
      tf.data.Dataset.from_tensor_slices(test_tensor_slices),
  )
