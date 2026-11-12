-- A LinkedIn batch now holds up to twelve posts, p1 to p12: Tin's own batches are six, and a
-- client's set can be longer. The post up next names one of them.
ALTER TABLE linkedin_up_next DROP CONSTRAINT linkedin_up_next_post_id_check;
ALTER TABLE linkedin_up_next
  ADD CONSTRAINT linkedin_up_next_post_id_check CHECK (post_id ~ '^p([1-9]|1[0-2])$');
