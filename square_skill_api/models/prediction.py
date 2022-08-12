import logging
from itertools import zip_longest
from typing import Dict, Iterable, List, Optional, Tuple, Union

import numpy as np
from pydantic import BaseModel, Field, root_validator

logger = logging.getLogger(__name__)

NO_ANSWER_FOUND_STRING = "No answer found."


class PredictionOutput(BaseModel):
    """Holds the output (e.g. an answer) and the score of that output."""

    output: str = Field(
        ...,
        description="The actual output of the model as string. "
        "Could be an answer for QA, an argument for AR or a label for Fact Checking.",
    )
    output_score: float = Field(..., description="The score assigned to the output.")


class PredictionDocument(BaseModel):
    """Holds a Document a prediction is based on."""

    index: str = Field(
        "", description="From which document store the document has been retrieved"
    )
    document_id: str = Field("", description="Id of the document in the index")
    document: str = Field(..., description="The text of the document")
    span: Optional[List[int]] = Field(
        None, description="Start and end character index of the span used. (optional)"
    )
    url: str = Field("", description="URL source of the document (if available)")
    source: str = Field("", description="The source of the document (if available)")
    document_score: float = Field(
        0, description="The score assigned to the document by retrieval"
    )


class Node(BaseModel):
    id: int
    name: str
    q_node: bool
    ans_node: bool
    weight: float


class Edge(BaseModel):
    source: int
    target: int
    weight: float
    label: str


class SubGraph(BaseModel):
    nodes: Dict[str, Node]
    edges: Dict[str, Edge]


class PredictionGraph(BaseModel):
    lm_subgraph: SubGraph
    attn_subgraph: SubGraph


class TokenAttribution(BaseModel):
    __root__: List = Field(
        ...,
        description="A list holding three items: (1) the index, (2) the word and (3) the score.",
    )


class Attributions(BaseModel):
    topk_question_idx: List[int]
    topk_context_idx: List[int]
    question_tokens: List[TokenAttribution]
    context_tokens: List[TokenAttribution]


class Adversarial(BaseModel):
    indices: List[int] = Field(None)


class Prediction(BaseModel):
    """A single prediction for a query."""

    question: str = Field(..., description="The question that was asked.")
    prediction_score: float = Field(
        ...,
        description="The overall score assigned to the prediction. Up to the Skill to decide how to calculate",
    )
    prediction_output: PredictionOutput = Field(
        ..., description="The prediction output of the skill."
    )
    prediction_documents: List[PredictionDocument] = Field(
        [],
        description="A list of the documents used by the skill to derive this prediction. "
        "Empty if no documents were used",
    )
    prediction_graph: Union[None, PredictionGraph] = Field(None)
    attributions: Union[None, Attributions] = Field(
        None, description="Feature attributions for the question and context"
    )


class QueryOutput(BaseModel):
    """The model for output that the skill returns after processing a query."""

    predictions: List[Prediction] = Field(
        ...,
        description="All predictions for the query. Predictions are sorted by prediction_score (descending)",
    )
    adversarial: Union[None, Adversarial] = Field(None)

    @staticmethod
    def sort_predictions_key(p: Union[Prediction, Dict]) -> Tuple:
        """Returns a key for soring predictions."""
        document_score = 1
        if isinstance(p, Prediction):
            answer_found = p.prediction_output.output not in [
                "",
                NO_ANSWER_FOUND_STRING,
            ]
            answer_score = p.prediction_score
            if p.prediction_documents:
                document_score = getattr(p.prediction_documents[0], "document_score", 1)
        elif isinstance(p, Dict):
            answer_found = p["prediction_output"]["output"] not in [
                "",
                NO_ANSWER_FOUND_STRING,
            ]
            answer_score = p["prediction_score"]
            if p["prediction_documents"]:
                document_score = p["prediction_documents"][0].get("document_score", 1)
        else:
            raise TypeError(type(p))
        return (answer_found, answer_score, document_score)

    @staticmethod
    def overwrite_from_model_api_output(
        model_api_output, key: str, value, len: int = None
    ) -> List[str]:
        """
        If `key` is given in the model_api_output, overwrite value with it.
        Else return provided value.
        """
        if key in model_api_output and model_api_output[key]:
            value = model_api_output[key]
        if isinstance(value, str):
            value = [value] * len
        return value

    @staticmethod
    def get_attribution_by_context_i(
        attributions: List[Dict[str, List[List[int]]]], context_i
    ) -> Attributions:

        attribution_keys = attributions[0].keys()
        context_attributions = {}
        for k in attribution_keys:
            context_attributions[k] = attributions[0][k][context_i]

        return Attributions.parse_obj(context_attributions)

    @root_validator
    def sort_predictions(cls, values):
        """Sorts predictions according the keys generated by `sort_prediction_key`. If
        `adversarial` are given, disable sorting.

        Args:
            v (List[Union[Prediction, Dict]]): List of unsorted predictions

        Returns:
            List[Union[Prediction, Dict]]: List of sorted predictions
        """

        if values["adversarial"] is None:
            values["predictions"] = sorted(
                values["predictions"], key=cls.sort_predictions_key, reverse=True
            )

        return values

    @staticmethod
    def _prediction_documents_iter_from_context(
        iter_len: int, context: Union[None, str, List[str]]
    ) -> Iterable[PredictionDocument]:
        """Generates an iterable for the context with `iter_len` length.

        Args:
            iter_len (int): Length of the iterable
            context (Union[None, str, List[str]]): If `None`, an iterable of empty lists
             will be generated. If `str`, the iterable will hold the same string for
             ever item. If `List[str]`, the iterable will loop over the items in the
             list.

        Raises:
            ValueError: Raises ValueError, if contex is a list with differnt size than
            `iter_len`.
            TypeError: Raises TypeError, if context is not `None`, `str` or `List[str]`.

        Returns:
            Iterable[PredictionDocument]: An iterable over list of PredictionDocumnets
        """
        if context is None:
            # no context for all answers
            prediction_documents_iter = ([] for _ in range(iter_len))
        elif isinstance(context, str):
            # same context for all answers
            prediction_documents_iter = (
                [PredictionDocument(document=context)] for _ in range(iter_len)
            )
        elif isinstance(context, list):
            # different context for all answers
            if len(context) != iter_len:
                raise ValueError()
            prediction_documents_iter = [
                [PredictionDocument(document=c)] for c in context
            ]
        else:
            raise TypeError(type(context))

        return prediction_documents_iter

    @staticmethod
    def extend_and_sort_attributions_to_scores(
        scores: List, attributions: List, fill_value=None
    ) -> List[List]:
        """extends attributios to same len as `scores and sorts according to `scores`"""
        # sort attributions by logits
        sort_idx = np.argsort(scores)[::-1]
        len_diff = len(scores) - len(attributions)
        if len_diff > 0:
            # extend attributions to be the same length as logits
            attributions.extend([fill_value] * len_diff)
            attributions = [x for _, x in sorted(zip(sort_idx, attributions))]
        return attributions

    @classmethod
    def from_sequence_classification(
        cls,
        questions: Union[str, List[str]],
        answers: List[str],
        model_api_output: Dict,
        context: Union[None, str, List[str]] = None,
    ):
        """Constructor for QueryOutput from sequence classification of model api.

        Args:
            answers (List[str]): List of answer strings
            model_api_output (Dict): Output returned from the model api.
            context (Union[None, str, List[str]], optional): Context used to obtain
            model api output. Defaults to None.
        """

        questions = cls.overwrite_from_model_api_output(
            model_api_output,
            key="questions",
            value=questions,
            len=len(model_api_output["model_outputs"]["logits"][0]),
        )
        context = cls.overwrite_from_model_api_output(
            model_api_output,
            key="context",
            value=context,
            len=len(model_api_output["model_outputs"]["logits"][0]),
        )

        # TODO: make this work with the datastore api output to support all
        # prediction_document fields
        prediction_documents_iter = cls._prediction_documents_iter_from_context(
            iter_len=len(answers), context=context
        )

        predictions = []
        predictions_scores = model_api_output["model_outputs"]["logits"][0]
        all_attributions = model_api_output.get("attributions", [])
        all_attributions = cls.extend_and_sort_attributions_to_scores(
            scores=predictions_scores, attributions=all_attributions
        )

        for (
            question,
            prediction_score,
            answer,
            prediction_documents,
            attributions,
        ) in zip_longest(
            questions,
            predictions_scores,
            answers,
            prediction_documents_iter,
            all_attributions,
            fillvalue=None,
        ):

            prediction_output = PredictionOutput(
                output=answer, output_score=prediction_score
            )

            prediction = Prediction(
                question=question,
                prediction_score=prediction_score,
                prediction_output=prediction_output,
                prediction_documents=prediction_documents,
            )
            if attributions:
                prediction.attributions = attributions

            predictions.append(prediction)

        if "adversarial" in model_api_output:
            predictions = cls(
                predictions=predictions, adversarial=model_api_output["adversarial"]
            )
        else:
            predictions = cls(predictions=predictions)

        return predictions

    @classmethod
    def from_sequence_classification_with_graph(
        cls,
        questions: Union[str, List[str]],
        answers: List[str],
        model_api_output: Dict,
    ):

        questions = cls.overwrite_from_model_api_output(
            model_api_output,
            key="questions",
            value=questions,
            len=len(model_api_output["model_outputs"]["logits"][0]),
        )
        predictions = []
        predictions_scores = model_api_output["model_outputs"]["logits"][0]
        for i, (question, prediction_score, answer) in enumerate(
            zip(questions, predictions_scores, answers)
        ):
            prediction_output = PredictionOutput(
                output=answer, output_score=prediction_score
            )
            prediction = Prediction(
                question=question,
                prediction_score=prediction_score,
                prediction_output=prediction_output,
            )

            if i == model_api_output["labels"][0]:
                # add subgraphs to the predicted answer
                prediction_graph = PredictionGraph(
                    lm_subgraph=model_api_output["lm_subgraph"],
                    attn_subgraph=model_api_output["attn_subgraph"],
                )
                prediction.prediction_graph = prediction_graph

            predictions.append(prediction)

        return cls(predictions=predictions)

    @classmethod
    def from_question_answering(
        cls,
        questions: Union[str, List[str]],
        model_api_output: Dict,
        context: Union[None, str, List[str]] = None,
        context_score: Union[None, float, List[float]] = None,
    ):
        """Constructor for QueryOutput from question answering of model api.

        Args:
            model_api_output (Dict): Output returned from the model api.
            context (Union[None, str, List[str]], optional): Context used to obtain
            model api output. Defaults to None.
            context_score (Union[None, float, List[float]], optional): Context scores
            from datastores.
        """
        logger.debug(f"input questions: {questions}")
        logger.debug(f"input context: {context}")

        questions = cls.overwrite_from_model_api_output(
            model_api_output,
            value=questions,
            key="questions",
            len=len(model_api_output["answers"]),
        )

        # TODO: make this work with the datastore api output to support all
        # prediction_document fields
        predictions: List[Prediction] = []

        attributions = model_api_output.get("attributions", None)
        logger.info(f"attributions: {attributions}")
        logger.info(f"questions: {questions}")
        logger.info(f"answers: {model_api_output['answers']}")
        # loop over contexts
        for i_context, (question, answers) in enumerate(
            zip(questions, model_api_output["answers"])
        ):
            if isinstance(context, list):
                context_doc_i = context[i_context]
                context_score_i = context_score[i_context]
            else:
                context_doc_i = "" if context is None else context
                context_score_i = 1 if context_score is None else context_score

            # get the sorted attributions for the answers from one doc
            scores = [answer["score"] for answer in answers]
            top_answer_idx = np.argmax(scores)

            logger.info(f"answers: {answers}")
            logger.info(f"scores: {scores}")
            # loop over answers per doc
            for i_answer, (answer, prediction_score) in enumerate(
                zip(
                    answers,
                    scores,
                )
            ):
                answer_str = answer["answer"]
                if not answer_str:
                    answer_str = NO_ANSWER_FOUND_STRING

                prediction_output = PredictionOutput(
                    output=answer_str, output_score=prediction_score
                )
                # NOTE: currently only one document per answer is supported
                prediction_documents = (
                    [
                        PredictionDocument(
                            document=context_doc_i,
                            span=[answer["start"], answer["end"]],
                            document_score=context_score_i,
                        )
                    ]
                    if context_doc_i
                    else []
                )
                prediction = Prediction(
                    question=question,
                    prediction_score=prediction_score,
                    prediction_output=prediction_output,
                    prediction_documents=prediction_documents,
                )
                if attributions and i_answer == top_answer_idx:
                    prediction.attributions = cls.get_attribution_by_context_i(
                        attributions, i_context
                    )
                logger.debug(f"prediction: {prediction}")
                predictions.append(prediction)

        if "adversarial" in model_api_output:
            predictions = cls(
                predictions=predictions, adversarial=model_api_output["adversarial"]
            )
        else:
            predictions = cls(predictions=predictions)

        return predictions

    @classmethod
    def from_generation(
        cls,
        questions: Union[str, List[str]],
        model_api_output: Dict,
        context: Union[None, str, List[str]] = None,
        context_score: Union[None, float, List[float]] = None,
    ):
        """Constructor for QueryOutput from generation of model api.

        Args:
            model_api_output (Dict): Output returned from the model api.
            context (Union[None, str, List[str]], optional): Context used to obtain
            model api output. Defaults to None.
            context_score (Union[None, float, List[float]], optional): Context scores
            from datastores.
        """
        questions = cls.overwrite_from_model_api_output(
            questions, model_api_output, len=len(model_api_output["generated_texts"][0])
        )

        predictions: List[Prediction] = []
        for answer, attributions in zip_longest(
            model_api_output["generated_texts"][0],
            model_api_output.get("attributions", []),
            fillvalue=None,
        ):
            # output_score is None for now
            prediction_output = PredictionOutput(output=answer, output_score=1)
            prediction = Prediction(
                prediction_score=1,
                prediction_output=prediction_output,
                prediction_documents=[PredictionDocument(document=context)],
            )
            if attributions:
                prediction.attributions = attributions

            predictions.append(prediction)

        return cls(predictions=predictions)
